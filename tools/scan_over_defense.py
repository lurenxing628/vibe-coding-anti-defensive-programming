"""反过度防御棘轮扫描器（over_defense 规则）。

扫描精度高的 6 类过度防御与残留死代码信号：

- internal_reflect_literal：生产代码 `getattr/hasattr/setattr/delattr(对象, "字面量", ...)`，
  对象不是本文件 import 进来的名字（对已导入模块/符号的反射豁免）。
- param_overwritten：函数参数在被读取前就被同名赋值覆盖——函数体（去掉 docstring）里第一条
  提到该参数的顶层语句是 `参数 = ...`（或元组解包/带注解赋值），且右侧不读它。
- zero_ref_func：生产代码中全仓同名唯一、名字不短于 4、非嵌套、无装饰器（装饰器视为框架注册）、
  非 dunder 的函数/方法，生产、tests/、宿主工具（tools/、scripts/ 的 Python）都没有引用，
  跨语言文本（前端、.cjs、模板、静态产物、打包脚本等）也搜不到。
- test_only_func：同上，但仅被 tests/ 引用。
- foreign_private_attr_assign：函数内往非 self/cls、非导入名的对象写私有属性 `obj._attr = ...`
  （含增量赋值、带值注解赋值、元组/列表解包目标）。obj 是本函数里刚构造出的新对象时豁免：`X.__new__(...)`、
  本类构造（`本类名(...)`、`cls(...)`、`type(...)(...)`、`self.__class__(...)`）、`copy(...)` 克隆。
- js_typeof_command_reset：前端目录下 `typeof command.reset === 'function'`（文本匹配）。

引用计数：按名字计 Load 的 Name、Attribute 属性名、标识符形态的字符串常量和
import 别名；同名多处定义的函数不判定。每个文件按 kind 计数；基线只减不增，刷新走 --refresh。

用法：
    python -m tools.scan_over_defense [--json] [--fail-on-new] [--refresh [--allow-growth]] [--quiet-when-clean]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Set, Tuple, Union

from tools.boundary_baseline import (
    BoundaryBaselineError,
    compare_counts,
    default_baseline_path,
    entries_to_counts,
    load_baseline,
    render_comparison,
    total_count,
    write_baseline,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
RULE = "over_defense"
PROD_ROOTS = ("src", "core", "data", "web", "app", "plugins", "desktop", "app.py", "main.py")
TEST_ROOTS = ("tests",)
TOOL_ROOTS = ("tools", "scripts")
FRONTEND_ROOT = "frontend"
FRONTEND_SUFFIXES = (".js", ".jsx")
SCAN_ROOTS = PROD_ROOTS + (FRONTEND_ROOT,)
XLANG_ROOTS = ("frontend", "tests", "templates", "static", "scripts", "packaging")
XLANG_SUFFIXES = (".js", ".jsx", ".cjs", ".mjs", ".html", ".json", ".bat", ".ps1", ".spec", ".txt")
ROOT_XLANG_SUFFIXES = (".bat", ".spec", ".toml", ".json")
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "vendor", ".venv"})
KINDS = (
    "internal_reflect_literal",
    "param_overwritten",
    "zero_ref_func",
    "test_only_func",
    "foreign_private_attr_assign",
    "js_typeof_command_reset",
)
REFLECT_CALLS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
JS_RESET_RE = re.compile(r"typeof\s+command\.reset\s*[!=]==?\s*['\"]function['\"]")
WORD_RE = re.compile(r"\w+")
MIN_DEF_NAME_LEN = 4
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
ASSIGN_TYPES = (ast.Assign, ast.AugAssign, ast.AnnAssign)
FuncNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]
AssignNode = Union[ast.Assign, ast.AugAssign, ast.AnnAssign]
Scoped = Tuple[ast.AST, Optional[FuncNode], Optional[ast.ClassDef]]
BASELINE_NOTE = (
    "反过度防御棘轮基线（内部反射字面量/参数进门覆盖/零引用与只被测试引用的函数/往外部对象写私有属性/"
    "前端 typeof command.reset）：条目只减不增，刷新走 --refresh。"
)


class Hit(NamedTuple):
    path: str
    kind: str
    line: int
    symbol: str


class DefSite(NamedTuple):
    path: str
    line: int
    is_class: bool
    nested: bool
    decorated: bool


def _rel(repo_root: str, path: str) -> str:
    return os.path.relpath(path, repo_root).replace("\\", "/")


def _iter_files(repo_root: str, roots: Sequence[str], suffixes: Tuple[str, ...]) -> List[str]:
    files: List[str] = []
    for root in roots:
        base = os.path.join(repo_root, root)
        if os.path.isfile(base):
            if base.endswith(suffixes):
                files.append(_rel(repo_root, base))
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for filename in sorted(filenames):
                if filename.endswith(suffixes):
                    files.append(_rel(repo_root, os.path.join(dirpath, filename)))
    return sorted(set(files))


def _parse(repo_root: str, rel_path: str) -> ast.Module:
    with open(os.path.join(repo_root, rel_path), encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=rel_path)


def _walk_scoped(tree: ast.AST) -> Iterator[Scoped]:
    """迭代遍历，给每个节点附上最近的外层函数与外层类（不含节点自身，与 scan.py 的父链口径一致）。"""
    stack: List[Scoped] = [(tree, None, None)]
    while stack:
        node, func, cls = stack.pop()
        yield node, func, cls
        inner_func = node if isinstance(node, FUNC_TYPES) else func
        inner_cls = node if isinstance(node, ast.ClassDef) else cls
        stack.extend((child, inner_func, inner_cls) for child in ast.iter_child_nodes(node))


def _count_reference(node: ast.AST, counter: Counter) -> None:
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        counter[node.id] += 1
    elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        counter[node.attr] += 1
    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
        counter[node.value] += 1
    elif isinstance(node, ast.alias):
        counter[node.name.split(".")[-1]] += 1


def _import_names(nodes: Iterable[ast.AST]) -> Set[str]:
    names: Set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _is_private_attr(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _qualname(fn: FuncNode, cls: Optional[ast.ClassDef]) -> str:
    return f"{cls.name}.{fn.name}" if cls is not None else fn.name


def _names_in(node: ast.AST) -> Set[str]:
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _reflect_hit(rel_path: str, call: ast.Call, imported: Set[str]) -> Optional[Hit]:
    if not (isinstance(call.func, ast.Name) and call.func.id in REFLECT_CALLS and len(call.args) >= 2):
        return None
    attr = call.args[1]
    if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
        return None
    target = call.args[0]
    if isinstance(target, ast.Name) and target.id in imported:
        return None
    return Hit(rel_path, "internal_reflect_literal", call.lineno, f"{call.func.id}:{attr.value}")


def _strip_docstring(body: List[ast.stmt]) -> List[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _assign_targets(node: AssignNode) -> List[ast.expr]:
    return list(node.targets) if isinstance(node, ast.Assign) else [node.target]


def _assigned_names(stmt: Union[ast.Assign, ast.AnnAssign]) -> Set[str]:
    targets = _assign_targets(stmt)
    names: Set[str] = set()
    for target in targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Tuple):
            names.update(elt.id for elt in target.elts if isinstance(elt, ast.Name))
    return names


def _param_overwritten(rel_path: str, fn: FuncNode, cls: Optional[ast.ClassDef]) -> List[Hit]:
    args = fn.args
    params = [arg.arg for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
              if arg.arg not in ("self", "cls")]
    if not params:
        return []
    body = _strip_docstring(fn.body)
    stmt_names = [_names_in(stmt) for stmt in body]
    hits: List[Hit] = []
    for param in params:
        for stmt, names in zip(body, stmt_names):
            if param not in names:
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None \
                    and param in _assigned_names(stmt) and param not in _names_in(stmt.value):
                hits.append(Hit(rel_path, "param_overwritten", stmt.lineno, f"{_qualname(fn, cls)}({param})"))
            break
    return hits


def _is_fresh_construction(call: ast.Call, owner: Optional[ast.ClassDef]) -> bool:
    """本函数刚造出的新对象：__new__、本类构造、type(x)(...)、x.__class__(...)、copy 克隆。"""
    callee = call.func
    if isinstance(callee, ast.Attribute):
        if callee.attr in ("__new__", "__class__"):
            return True
        return callee.attr == "copy" and isinstance(callee.value, ast.Name) and callee.value.id == "copy"
    if isinstance(callee, ast.Name):
        return callee.id in ("cls", "copy") or (owner is not None and callee.id == owner.name)
    return isinstance(callee, ast.Call) and isinstance(callee.func, ast.Name) and callee.func.id == "type"


def _fresh_object_names(fn: FuncNode, owner: Optional[ast.ClassDef]) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and _is_fresh_construction(node.value, owner):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _flat_targets(node: AssignNode) -> Iterator[ast.expr]:
    pending = _assign_targets(node)
    while pending:
        target = pending.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        else:
            yield target


def _foreign_private_targets(node: AssignNode, imported: Set[str]) -> List[Tuple[str, str]]:
    """返回 (对象名, 私有属性名)：往非 self/cls、非导入名的对象上写单下划线属性（含解包目标）。"""
    if isinstance(node, ast.AnnAssign) and node.value is None:
        return []
    found: List[Tuple[str, str]] = []
    for target in _flat_targets(node):
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                and target.value.id not in ("self", "cls") and target.value.id not in imported \
                and _is_private_attr(target.attr):
            found.append((target.value.id, target.attr))
    return found


def _scan_prod_tree(
    rel_path: str,
    tree: ast.Module,
    hits: List[Hit],
    defs: Dict[str, List[DefSite]],
    refs: Counter,
) -> None:
    nodes = list(_walk_scoped(tree))
    imported = _import_names(node for node, _func, _cls in nodes)
    owner_class: Dict[int, Optional[ast.ClassDef]] = {}
    fresh_names: Dict[int, Set[str]] = {}
    for node, func, cls in nodes:
        _count_reference(node, refs)
        if isinstance(node, DEF_TYPES):
            if isinstance(node, FUNC_TYPES):
                owner_class[id(node)] = cls
                hits.extend(_param_overwritten(rel_path, node, cls))
            if not _is_dunder(node.name):
                defs[node.name].append(DefSite(rel_path, node.lineno, isinstance(node, ast.ClassDef),
                                               func is not None, bool(node.decorator_list)))
        elif isinstance(node, ast.Call):
            hit = _reflect_hit(rel_path, node, imported)
            if hit is not None:
                hits.append(hit)
        elif func is not None and isinstance(node, ASSIGN_TYPES):
            targets = _foreign_private_targets(node, imported)
            if not targets:
                continue
            if id(func) not in fresh_names:
                fresh_names[id(func)] = _fresh_object_names(func, owner_class[id(func)])
            for owner, attr in targets:
                if owner not in fresh_names[id(func)]:
                    hits.append(Hit(rel_path, "foreign_private_attr_assign", node.lineno, f"{owner}.{attr}"))


def _cross_language_words(repo_root: str) -> Set[str]:
    paths = [os.path.join(repo_root, rel) for rel in _iter_files(repo_root, XLANG_ROOTS, XLANG_SUFFIXES)]
    paths.extend(
        os.path.join(repo_root, name) for name in sorted(os.listdir(repo_root))
        if name.endswith(ROOT_XLANG_SUFFIXES) and os.path.isfile(os.path.join(repo_root, name))
    )
    words: Set[str] = set()
    for path in paths:
        # 批处理等可能不是 UTF-8；这里只找 ASCII 标识符，坏字节替换成非单词字符不影响匹配。
        with open(path, encoding="utf-8", errors="replace") as handle:
            words.update(WORD_RE.findall(handle.read()))
    return words


def _reference_hits(
    repo_root: str,
    defs: Dict[str, List[DefSite]],
    refs_prod: Counter,
    refs_test: Counter,
    refs_tool: Counter,
) -> List[Hit]:
    hits: List[Hit] = []
    xlang_words: Optional[Set[str]] = None
    for name, sites in sorted(defs.items()):
        if len(sites) != 1 or len(name) < MIN_DEF_NAME_LEN:
            continue
        site = sites[0]
        if site.is_class or site.nested or site.decorated or refs_prod[name] or refs_tool[name]:
            continue
        if xlang_words is None:
            xlang_words = _cross_language_words(repo_root)
        if name in xlang_words:
            continue
        kind = "test_only_func" if refs_test[name] else "zero_ref_func"
        hits.append(Hit(site.path, kind, site.line, name))
    return hits


def _frontend_hits(repo_root: str, rel_path: str) -> List[Hit]:
    with open(os.path.join(repo_root, rel_path), encoding="utf-8") as handle:
        text = handle.read()
    return [
        Hit(rel_path, "js_typeof_command_reset", text.count("\n", 0, match.start()) + 1, "command.reset")
        for match in JS_RESET_RE.finditer(text)
    ]


def _entries(hits: Iterable[Hit]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[Hit]] = defaultdict(list)
    for hit in hits:
        grouped[(hit.path, hit.kind)].append(hit)
    return [
        {
            "path": path,
            "kind": kind,
            "count": len(items),
            "lines": sorted(item.line for item in items),
            "symbols": sorted({item.symbol for item in items}),
        }
        for (path, kind), items in sorted(grouped.items())
    ]


def _resolve_prod_roots(repo_root: str) -> Tuple[str, ...]:
    existing = tuple(r for r in PROD_ROOTS if os.path.exists(os.path.join(repo_root, r)))
    if existing:
        return existing
    ignored = set(SKIP_DIRS) | set(TEST_ROOTS) | set(TOOL_ROOTS) | {".git", ".checkup", "docs", "dist", "build"}
    return tuple(
        name for name in sorted(os.listdir(repo_root))
        if not name.startswith(".") and name not in ignored
        and (os.path.isdir(os.path.join(repo_root, name)) or name.endswith(".py"))
    )


def scan(repo_root: str = REPO_ROOT) -> Dict[str, Any]:
    hits: List[Hit] = []
    defs: Dict[str, List[DefSite]] = defaultdict(list)
    refs_prod: Counter = Counter()
    refs_test: Counter = Counter()
    refs_tool: Counter = Counter()
    prod_roots = _resolve_prod_roots(repo_root)
    prod_files = _iter_files(repo_root, prod_roots, (".py",))
    for rel_path in prod_files:
        _scan_prod_tree(rel_path, _parse(repo_root, rel_path), hits, defs, refs_prod)
    reference_files = 0
    for roots, counter in ((TEST_ROOTS, refs_test), (TOOL_ROOTS, refs_tool)):
        for rel_path in _iter_files(repo_root, roots, (".py",)):
            reference_files += 1
            for node in ast.walk(_parse(repo_root, rel_path)):
                _count_reference(node, counter)
    hits.extend(_reference_hits(repo_root, defs, refs_prod, refs_test, refs_tool))
    frontend_files = _iter_files(repo_root, (FRONTEND_ROOT,), FRONTEND_SUFFIXES)
    for rel_path in frontend_files:
        hits.extend(_frontend_hits(repo_root, rel_path))
    entries = _entries(hits)
    kind_totals = {kind: 0 for kind in KINDS}
    for entry in entries:
        kind_totals[entry["kind"]] += entry["count"]
    return {
        "rule": RULE,
        "scan_roots": list(SCAN_ROOTS),
        "file_count": len(prod_files) + len(frontend_files),
        "reference_file_count": reference_files,
        "kind_totals": kind_totals,
        "entries": entries,
        "total": sum(kind_totals.values()),
    }


def render_text(result: Dict[str, Any]) -> str:
    entries = list(result["entries"])
    lines = [f"[{RULE}] 扫描 {result['file_count']} 个文件，反过度防御信号 {result['total']} 处，涉及 {len(entries)} 个条目"]
    for kind, count in result["kind_totals"].items():
        lines.append(f"  {kind:28s} {count:4d}")
    for entry in sorted(entries, key=lambda item: (-int(item["count"]), str(item["path"]), str(item["kind"])))[:15]:
        lines.append(f"  {entry['count']:3d}  {entry['path']}  [{entry['kind']}]  ({', '.join(entry['symbols'][:5])})")
    return "\n".join(lines)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="反过度防御棘轮扫描器")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-new", action="store_true", help="相对基线有新增/增加/失效条目即退出码 1；基线异常退出码 2")
    parser.add_argument("--refresh", action="store_true", help="按当前扫描结果重写基线（默认拒绝总命中数增长）")
    parser.add_argument("--allow-growth", action="store_true")
    parser.add_argument("--quiet-when-clean", action="store_true")
    parser.add_argument("--repo-root", default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(list(argv) if argv is not None else None)


def _error(message: str) -> None:
    print(f"[over-defense] {message}", file=sys.stderr, flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        result = scan(args.repo_root)
    except (SyntaxError, UnicodeDecodeError) as exc:
        _error(f"源码解析失败，门禁已阻断：{exc}")
        return 2
    current = entries_to_counts(result["entries"])
    baseline_path = default_baseline_path(args.repo_root, RULE)
    if args.refresh:
        try:
            previous = load_baseline(baseline_path, expected_rule=RULE)
        except BoundaryBaselineError as exc:
            _error(str(exc))
            return 2
        if previous is not None and total_count(current) > total_count(previous) and not args.allow_growth:
            _error(f"刷新被拒绝：总命中数 {total_count(previous)} -> {total_count(current)} 增长；请先消除新增债务，或人工核对后加 --allow-growth")
            return 1
        write_baseline(baseline_path, rule=RULE, scan_roots=list(SCAN_ROOTS), note=BASELINE_NOTE, counts=current)
        print(f"[{RULE}] 基线已写入 {os.path.relpath(baseline_path, args.repo_root)}：{len(current)} 条目 / {total_count(current)} 处")
        return 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.fail_on_new:
        print(render_text(result), flush=True)
        return 0
    try:
        baseline = load_baseline(baseline_path, expected_rule=RULE)
    except BoundaryBaselineError as exc:
        _error(str(exc))
        return 2
    comparison = compare_counts(current, baseline)
    if comparison.baseline_missing:
        _error(f"基线缺失：{baseline_path}；请先查看扫描结果并受控执行 --refresh")
        return 2
    if comparison.clean and args.quiet_when_clean:
        return 0
    for line in render_comparison(RULE, comparison):
        print(line, flush=True)
    return 1 if comparison.has_debt else 0


if __name__ == "__main__":
    raise SystemExit(main())
