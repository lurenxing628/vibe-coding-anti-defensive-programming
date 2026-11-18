from __future__ import annotations

import ast
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple, Union

LoaderCandidates = Dict[str, Set[str]]
StaticValue = Union[str, Path]

# import 四分类按加载期强度从强到弱排列；运行时图 = hard + cond + lazy（typeonly 运行时不执行）。
IMPORT_KINDS = ("hard", "cond", "lazy", "typeonly")
RUNTIME_KINDS = ("hard", "cond", "lazy")


class ResolvedFileImport(NamedTuple):
    target: str
    package: Optional[str]
    context: str
    line: int


class ResolvedNameImport(NamedTuple):
    target: str
    package: Optional[str]
    context: str
    line: int
    scope: str
    loader_kind: str
    call_ast: str


class UnresolvedDynamicImport(NamedTuple):
    context: str
    line: int
    scope: str
    loader_kind: str
    expression: str
    call_ast: str

    def to_record(self, file_rel: str) -> Dict[str, Union[str, int]]:
        return {
            "file": file_rel,
            "line": self.line,
            "scope": self.scope,
            "context": self.context,
            "loader_kind": self.loader_kind,
            "expression": self.expression,
            "call_ast": self.call_ast,
        }


def canonical_call_ast(node: object) -> str:
    if isinstance(node, ast.Index):
        return canonical_call_ast(next(ast.iter_child_nodes(node)))
    if isinstance(node, ast.ExtSlice):
        return f"Tuple(elts=[{', '.join(canonical_call_ast(item) for item in ast.iter_child_nodes(node))}])"
    if isinstance(node, ast.Constant):
        return f"Constant(value={node.value!a})"
    if isinstance(node, ast.AST):
        fields = [
            f"{name}={canonical_call_ast(value)}"
            for name, value in ast.iter_fields(node)
            if name != "ctx"
        ]
        return f"{node.__class__.__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return f"[{', '.join(canonical_call_ast(item) for item in node)}]"
    return ascii(node)


def loader_import_bindings(node: ast.AST) -> List[Tuple[str, Optional[str]]]:
    if isinstance(node, ast.Import):
        return [(alias.asname or alias.name.split(".")[0], _import_kind(alias)) for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    kinds = {
        ("importlib", "import_module"): "import_module",
        ("importlib", "util"): "importlib.util",
        ("importlib.util", "spec_from_file_location"): "spec_from_file_location",
        ("pathlib", "Path"): "Path",
    }
    return [
        (alias.asname or alias.name, kinds.get((node.module or "", alias.name)) if node.level == 0 else None)
        for alias in node.names if alias.name != "*"
    ]


def _import_kind(alias: ast.alias) -> Optional[str]:
    if alias.name in {"importlib", "pathlib"}:
        return alias.name
    if alias.name == "importlib.util" and alias.asname:
        return "importlib.util"
    if alias.name.startswith("importlib.") and alias.asname is None:
        return "importlib"
    return None


def _scope_nodes(scope: ast.AST) -> List[ast.AST]:
    roots: Sequence[ast.AST] = [scope.body] if isinstance(scope, ast.Lambda) else scope.body  # type: ignore[attr-defined]
    pending = list(reversed(roots))
    nodes: List[ast.AST] = []
    while pending:
        node = pending.pop()
        nodes.append(node)
        children = list(ast.iter_child_nodes(node))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body_ids = {id(child) for child in node.body}
            children = [child for child in children if id(child) not in body_ids]
        elif isinstance(node, ast.Lambda):
            children = [child for child in children if child is not node.body]
        pending.extend(reversed(children))
    return nodes


def _written_names(node: ast.AST) -> Set[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
        while isinstance(node, ast.Attribute):
            node = node.value
        return {node.id} if isinstance(node, ast.Name) else set()
    return set()


def _parameter_names(scope: ast.AST) -> Set[str]:
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    args = scope.args
    parameters = args.posonlyargs + args.args + args.kwonlyargs + [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    return {arg.arg for arg in parameters}


@dataclass
class ImportScope:
    bound: Set[str]
    candidates: LoaderCandidates
    aliases: Dict[str, str]
    assignments: Dict[str, ast.AST]
    wildcard: bool


def import_scope(scope: ast.AST) -> ImportScope:
    trusted: Dict[str, List[str]] = defaultdict(list)
    writes = Counter(_parameter_names(scope))
    nodes = _scope_nodes(scope)
    for node in nodes:
        writes.update(_written_names(node))
        for name, kind in loader_import_bindings(node):
            writes[name] += 1
            if kind is not None:
                trusted[name].append(kind)
    writes.update(_external_names(scope))
    wildcard = any(isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names) for node in nodes)
    candidates = {name: set(kinds) for name, kinds in trusted.items()}
    aliases = {
        name: kinds[0] for name, kinds in trusted.items()
        if len(set(kinds)) == 1 and writes[name] == len(kinds) and not wildcard
    }
    assignments = {} if wildcard else _single_assignments(scope, writes)
    return ImportScope(set(writes), candidates, aliases, assignments, wildcard)


def _external_names(scope: ast.AST) -> Set[str]:
    # A nested writer can change an enclosing binding before a deferred import.
    return {name for node in ast.walk(scope) if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names}


def _single_assignments(scope: ast.AST, writes: Dict[str, int]) -> Dict[str, ast.AST]:
    assignments: Dict[str, ast.AST] = {}
    body = scope.body if isinstance(scope, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else []
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and writes.get(target.id) == 1:
            assignments[target.id] = value
    return assignments


def static_import_value(node: ast.AST, values: Dict[str, StaticValue], aliases: Dict[str, str]) -> Optional[StaticValue]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.Call):
        return _static_path_call(node, values, aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = static_import_value(node.left, values, aliases)
        right = static_import_value(node.right, values, aliases)
        return left / right if isinstance(left, Path) and isinstance(right, str) else None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = static_import_value(node.value, values, aliases)
        return base.parent if isinstance(base, Path) else None
    if isinstance(node, ast.Subscript):
        return _static_parent(node, values, aliases)
    return None


def _static_path_call(node: ast.Call, values: Dict[str, StaticValue], aliases: Dict[str, str]) -> Optional[Path]:
    if node.keywords:
        return None
    func = node.func
    constructor = isinstance(func, ast.Name) and aliases.get(func.id) == "Path"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        constructor = func.attr == "Path" and aliases.get(func.value.id) == "pathlib"
    if constructor and len(node.args) == 1:
        value = static_import_value(node.args[0], values, aliases)
        return Path(value) if value is not None else None
    if isinstance(func, ast.Attribute) and func.attr == "resolve" and not node.args:
        base = static_import_value(func.value, values, aliases)
        if isinstance(base, Path) and base.is_absolute():
            try:
                return base.resolve()
            except (OSError, RuntimeError, ValueError):
                return None
    return None


def _static_parent(node: ast.Subscript, values: Dict[str, StaticValue], aliases: Dict[str, str]) -> Optional[Path]:
    if not isinstance(node.value, ast.Attribute) or node.value.attr != "parents":
        return None
    base = static_import_value(node.value.value, values, aliases)
    index = next(ast.iter_child_nodes(node.slice)) if isinstance(node.slice, ast.Index) else node.slice
    if not isinstance(base, Path) or not isinstance(index, ast.Constant) or type(index.value) is not int:
        return None
    return base.parents[index.value] if 0 <= index.value < len(base.parents) else None


def source_file_target(value: Optional[StaticValue], file_to_mod: Dict[str, str]) -> Optional[str]:
    if value is None or not os.path.isabs(value):
        return None
    try:
        source = str(Path(value).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    return file_to_mod.get(source)


@dataclass
class _TarjanState:
    graph: Dict[str, List[str]]
    index: Dict[str, int] = field(default_factory=dict)
    low: Dict[str, int] = field(default_factory=dict)
    on_stack: Dict[str, bool] = field(default_factory=dict)
    stack: List[str] = field(default_factory=list)
    components: List[List[str]] = field(default_factory=list)
    counter: int = 0

    def nodes(self) -> List[str]:
        all_nodes = set(self.graph) | {target for targets in self.graph.values() for target in targets}
        return list(all_nodes)


def tarjan(graph: Dict[str, List[str]]) -> List[List[str]]:
    state = _TarjanState(graph=graph)
    for node in state.nodes():
        if node not in state.index:
            _strongconnect_iterative(state, node)
    return state.components


def _strongconnect_iterative(state: _TarjanState, root: str) -> None:
    work: List[Tuple[str, int]] = [(root, 0)]
    while work:
        node, next_index = work[-1]
        if node not in state.index:
            _visit_node(state, node)
        child, updated_index = _next_unvisited_child(state, node, next_index)
        if child is not None:
            work[-1] = (node, updated_index)
            work.append((child, 0))
            continue
        _finish_node(state, node)
        work.pop()
        if work:
            parent = work[-1][0]
            state.low[parent] = min(state.low[parent], state.low[node])


def _visit_node(state: _TarjanState, node: str) -> None:
    state.index[node] = state.low[node] = state.counter
    state.counter += 1
    state.stack.append(node)
    state.on_stack[node] = True


def _next_unvisited_child(state: _TarjanState, node: str, start_index: int) -> Tuple[Optional[str], int]:
    neighbors = state.graph.get(node, ())
    index = start_index
    while index < len(neighbors):
        child = neighbors[index]
        if child not in state.index:
            return child, index + 1
        if state.on_stack.get(child):
            state.low[node] = min(state.low[node], state.index[child])
        index += 1
    return None, index


def _finish_node(state: _TarjanState, node: str) -> None:
    if state.low[node] != state.index[node]:
        return
    component: List[str] = []
    while state.stack:
        member = state.stack.pop()
        state.on_stack[member] = False
        component.append(member)
        if member == node:
            break
    if len(component) > 1:
        state.components.append(component)


def module_from_path(rel: str) -> str:
    parts = rel[:-3].replace("\\", "/").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def current_package_parts(path: str, module: str) -> List[str]:
    parts = module.split(".")
    return parts if os.path.basename(path) == "__init__.py" else parts[:-1]


def target_directory(module: str, mod_to_file: Dict[str, str], repo_root: str) -> Optional[str]:
    path = mod_to_file.get(module)
    if not path:
        return None
    return os.path.dirname(os.path.relpath(path, repo_root)).replace("\\", "/") or "."


def adjacency(edge_map: Dict[Tuple[str, str], list]) -> Dict[str, List[str]]:
    rows: Dict[str, set] = defaultdict(set)
    for source, target in edge_map:
        rows[source].add(target)
    return {source: list(targets) for source, targets in rows.items()}


def merge_edges(
    edges_by_kind: Mapping[str, Mapping[Tuple[str, str], list]],
    kinds: Sequence[str],
) -> Dict[Tuple[str, str], list]:
    """把选定类别的边合成一张图；每个位置末尾追加它的 import 类别，供圈内边记录判定类别。"""
    merged: Dict[Tuple[str, str], list] = defaultdict(list)
    for kind in kinds:
        for edge, locations in edges_by_kind[kind].items():
            merged[edge].extend(location + (kind,) for location in locations)
    return merged


def _module_edge_record(source_mod: str, target_mod: str, locations: List[Tuple[str, int, str]]) -> dict:
    # 一条模块边的类别取它所有位置里加载期最强的那类；证据位置取该类里排序最前的一处。
    kind = min((location_kind for _, _, location_kind in locations), key=IMPORT_KINDS.index)
    rel, line = min((rel, line) for rel, line, location_kind in locations if location_kind == kind)
    return {
        "source": source_mod,
        "target": target_mod,
        "kind": kind,
        "src": rel,
        "line": line,
        "extra": len(locations) - 1,
    }


def dir_cycle_records(sccs: List[List[str]], dmap: Dict[Tuple[str, str], list]) -> List[dict]:
    records: List[dict] = []
    for component in sorted(sccs, key=len, reverse=True):
        members = set(component)
        by_module_edge: Dict[Tuple[str, str], list] = defaultdict(list)
        for (source_dir, target_dir), locations in dmap.items():
            if source_dir not in members or target_dir not in members:
                continue
            for rel, line, target_mod, kind in locations:
                by_module_edge[(module_from_path(rel), target_mod)].append((rel, line, kind))
        evidence = [
            _module_edge_record(source_mod, target_mod, locations)
            for (source_mod, target_mod), locations in sorted(by_module_edge.items())
        ]
        records.append({"members": sorted(component), "edges": evidence})
    return records


def file_cycle_records(sccs: List[List[str]], fmap: Dict[Tuple[str, str], list]) -> List[dict]:
    records: List[dict] = []
    for component in sorted(sccs, key=len, reverse=True):
        members = set(component)
        evidence = [
            _module_edge_record(source_mod, target_mod, locations)
            for (source_mod, target_mod), locations in sorted(fmap.items())
            if source_mod in members and target_mod in members
        ]
        records.append({"members": sorted(component), "edges": evidence})
    return records


def delayed_dir_cycle_records(
    runtime_sccs: List[List[str]],
    hard_sccs: List[List[str]],
    runtime_dir: Dict[Tuple[str, str], list],
) -> List[dict]:
    hard_sets = {frozenset(component) for component in hard_sccs}
    return dir_cycle_records(
        [component for component in runtime_sccs if frozenset(component) not in hard_sets],
        runtime_dir,
    )


def render_text(result: dict) -> str:
    lines: List[str] = []
    append = lines.append
    edge_counts = result["edge_counts"]
    append("=" * 70)
    append(f"[import-cycles] 模块文件数:{result['module_count']} | 解析失败:{len(result['parse_errors'])}")
    append(
        f"[import-cycles] 边计数  hard:{edge_counts['hard']} cond:{edge_counts['cond']} "
        f"lazy:{edge_counts['lazy']} typeonly:{edge_counts['typeonly']}"
    )
    append(f"[import-cycles] 父包初始化隐式边:{len(result['parent_package_init_edges'])}")
    for row in result["parent_package_init_edges"][:20]:
        append(f"  ^ {row['file']}:{row['line']} [{row['context']}] {row['source']} -> {row['target']}")
    if len(result["parent_package_init_edges"]) > 20:
        append(f"  ^ ... 另有 {len(result['parent_package_init_edges']) - 20} 条，完整清单见 JSON")
    append(f"[import-cycles] 未解析动态导入:{len(result['unresolved_dynamic_imports'])}")
    for row in result["unresolved_dynamic_imports"]:
        append(f"  ? {row['file']}:{row['line']} [{row['context']}] {row['expression']}")
    for row in result["parse_errors"]:
        append(f"  ! {row['file']}: {row['error']}")
    append("=" * 70)
    append("")
    append(f"① 硬加载期目录环(只 hard 边):{len(result['hard_dir_cycles'])} 个")
    for cycle in result["hard_dir_cycles"]:
        append(f"  [size={len(cycle['members'])}] {' ⇄ '.join(cycle['members'])}")
        for edge in cycle["edges"][:8]:
            extra = f"  (+{edge['extra']})" if edge["extra"] else ""
            append(f"      {edge['src']}:{edge['line']} -> {edge['target']}{extra}")
    append("")
    append(
        f"② Python 加载文件环(hard 显式边 + 父包初始化边):{len(result['hard_file_cycles'])} 个；"
        f"其中纯显式 import 文件环:{len(result['explicit_hard_file_cycles'])} 个"
    )
    for cycle in result["hard_file_cycles"]:
        append(f"  {cycle}")
    append("")
    append(
        f"③ 延迟目录环(运行时图相对硬加载期图新增;函数体内导入只推迟加载,闭合的环同样计入基线):"
        f"{len(result['delayed_dir_cycles'])} 个"
    )
    for cycle in result["delayed_dir_cycles"]:
        append(f"  [size={len(cycle['members'])}] {' ⇄ '.join(cycle['members'])}")
        for edge in cycle["edges"][:4]:
            append(f"      {edge['src']}:{edge['line']} -> {edge['target']}")
    append("")
    append(
        f"[import-cycles] 运行时文件环:{result['runtime_file_cycle_count']} 个"
        f"(对比硬加载期文件环 {len(result['hard_file_cycles'])} 个；"
        f"纯显式运行时文件环 {len(result['explicit_runtime_file_cycles'])} 个)"
    )
    for cycle in result["runtime_file_cycles"]:
        append(f"  [size={len(cycle['members'])}] {' ⇄ '.join(cycle['members'])}")
        for edge in cycle["edges"][:4]:
            append(f"      {edge['src']}:{edge['line']} -> {edge['target']}")
    return "\n".join(lines)
