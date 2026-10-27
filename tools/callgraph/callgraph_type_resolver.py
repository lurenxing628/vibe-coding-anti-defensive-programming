"""调用图两档消歧器：轻量 AST 类型推断（Tier 1） + 重型 SCIP / Runtime 融合（Tier 2）。

- Tier 1（默认开启，0 外部依赖，毫秒级）：
  利用类继承表、函数参数/返回值类型注解、类属性注解与 __init__ 赋值（self.x = Cls()）、
  跨函数调用点实参类型传播（2 轮不动点）、局部变量构造/注解/with 绑定、super() 与 @property 访问，
  将 ``obj.method()``、``self.attr.method()`` 与 ``obj.prop`` 消解为 ``kind="typed", ambiguous=False``
  的确信边，同时剔除向全仓其他同名类乱连的假模糊边。
- Tier 2（--mode deep 开启）：
  融合 SCIP 全量类型索引（index.scip）与 sys.setprofile 运行时真实调用边（runtime_edges.json），
  将命中的模糊边直接升级为 ``ambiguous=False`` 确信边，并补齐纯动态派发边。
"""
from __future__ import annotations

import ast
import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import callgraph_call_sites as _callsites

AttrCall = Tuple[str, str, int]
Edge = Dict[str, Any]

BUILTIN_TYPE_TAG = "__builtin__"
BUILTIN_CONSTRUCTORS = frozenset({
    "list", "dict", "set", "frozenset", "tuple", "str", "bytes", "bytearray",
    "int", "float", "bool", "defaultdict", "Counter", "deque", "OrderedDict",
    "Path", "PurePath", "PosixPath", "WindowsPath", "datetime", "date", "time", "timedelta",
    "re.compile", "io.StringIO", "io.BytesIO", "StringIO", "BytesIO",
    "sqlite3.connect", "logging.getLogger", "getLogger",
})
BUILTIN_ANNOTATIONS = frozenset({
    "list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection",
    "dict", "Dict", "Mapping", "MutableMapping", "Counter", "defaultdict", "OrderedDict",
    "set", "Set", "AbstractSet", "MutableSet", "frozenset", "FrozenSet",
    "tuple", "Tuple", "str", "bytes", "bytearray", "int", "float", "bool",
    "Path", "PurePath", "datetime", "date", "time", "timedelta", "Pattern",
    "Callable", "Connection", "Cursor", "Logger", "TextIO", "BinaryIO", "IO",
})
CONTAINER_METHODS = frozenset({
    "append", "extend", "insert", "pop", "remove", "clear", "copy", "sort", "reverse", "index", "count",
    "get", "keys", "values", "items", "setdefault", "update", "popitem",
    "add", "discard", "union", "intersection", "difference",
    "strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "join",
    "replace", "lower", "upper", "startswith", "endswith", "find", "rfind",
    "encode", "decode", "format", "isascii", "isdigit", "isdecimal", "isalnum", "isalpha",
    "exists", "is_file", "is_dir", "read_text", "write_text", "read_bytes", "write_bytes",
    "mkdir", "unlink", "resolve", "glob", "rglob", "with_name", "with_suffix",
    "isoformat", "fromisoformat", "total_seconds", "strftime", "strptime",
    "close", "fetchone", "fetchall", "fetchmany", "commit", "rollback", "cursor",
    "execute", "executemany", "backup", "iter_rows", "set", "partition", "rpartition",
    "info", "debug", "warning", "error", "exception", "group", "groups",
})
_WRAPPER_TYPES = ("Optional", "Union", "Annotated", "Final", "ClassVar")


def _unwrap_annotation(node: Optional[ast.AST]) -> str:
    """从类型注解 AST 中提取核心类名或泛型容器名（剥离 Optional / Union / Annotated / 引号）。"""
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value.strip()
        if "[" in text:
            head, _, inner = text.partition("[")
            short_head = head.split(".")[-1].strip()
            if short_head in _WRAPPER_TYPES:
                first = inner.rstrip("]").split(",")[0].strip()
                return _unwrap_annotation(ast.Constant(value=first))
            return short_head
        if "|" in text:
            parts = [p.strip() for p in text.split("|") if p.strip() and p.strip() != "None"]
            return _unwrap_annotation(ast.Constant(value=parts[0])) if parts else ""
        return text
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _unwrap_annotation(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        wrapper = _unwrap_annotation(node.value)
        short_wrapper = wrapper.split(".")[-1]
        if short_wrapper in _WRAPPER_TYPES:
            slc = node.slice
            if isinstance(slc, ast.Index):  # Py3.8 兼容
                slc = slc.value  # type: ignore[attr-defined]
            if isinstance(slc, ast.Tuple) and slc.elts:
                for elt in slc.elts:
                    name = _unwrap_annotation(elt)
                    if name and name != "None":
                        return name
            return _unwrap_annotation(slc)
        return short_wrapper or wrapper
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _unwrap_annotation(node.left)
        return left if left and left != "None" else _unwrap_annotation(node.right)
    return ""


def _extract_dataclass_default_factory(expr: Optional[ast.AST]) -> str:
    """提取 ``field(default_factory=...)`` 中的工厂类名或构造器名。"""
    if not isinstance(expr, ast.Call):
        return ""
    callee = _callsites.receiver_text(expr.func).split(".")[-1]
    if callee != "field":
        return ""
    for kw in expr.keywords:
        if kw.arg == "default_factory":
            return _callsites.receiver_text(kw.value)
    return ""


def _is_property_node(fnode: ast.AST) -> bool:
    if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in fnode.decorator_list:
        dname = _callsites.receiver_text(dec).split(".")[-1]
        if dname in ("property", "cached_property"):
            return True
    return False


def _node_bound_names(fnode: ast.AST) -> Set[str]:
    bound: Set[str] = set()
    if isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = (
            list(getattr(fnode.args, "posonlyargs", []))
            + list(fnode.args.args)
            + list(fnode.args.kwonlyargs)
        )
        for arg in args:
            bound.add(arg.arg)
        if fnode.args.vararg:
            bound.add(fnode.args.vararg.arg)
        if fnode.args.kwarg:
            bound.add(fnode.args.kwarg.arg)
    for sub in _callsites.walk_runtime_body(fnode):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            bound.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and isinstance(sub.name, str):
            bound.add(sub.name)
    return bound


def local_bound_names(
    info: Dict[str, Any],
    all_funcs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Set[str]:
    """收集当前函数及外层闭包祖先函数的形参与局部赋值绑定名。"""
    fnode = info["node"] if isinstance(info, dict) else info
    bound = _node_bound_names(fnode)
    if isinstance(info, dict) and all_funcs:
        parent_qual = info.get("parent")
        while parent_qual and parent_qual in all_funcs:
            pinfo = all_funcs[parent_qual]
            bound.update(_node_bound_names(pinfo["node"]))
            parent_qual = pinfo.get("parent")
    return bound


def module_top_aliases(tree: Optional[ast.Module], base_imports: Dict[str, str]) -> Dict[str, str]:
    """提取模块顶层 ``alias = imported_mod.func`` 形式的导入别名。"""
    if tree is None:
        return {}
    aliases: Dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Attribute) and isinstance(stmt.value.value, ast.Name):
            mod = base_imports.get(stmt.value.value.id)
            if mod:
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name):
                        aliases[tgt.id] = f"{mod}.{stmt.value.attr}"
    return aliases


def collect_value_references(fnode: ast.AST) -> Set[str]:
    """收集函数体内作为值传递/返回/装入容器的裸名（补充 call_argument_references）。"""
    refs = set(_callsites.call_argument_references(fnode))
    for sub in _callsites.walk_runtime_body(fnode):
        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Name):
            refs.add(sub.value.id)
        elif isinstance(sub, (ast.Tuple, ast.List, ast.Set)):
            for elt in sub.elts:
                if isinstance(elt, ast.Name):
                    refs.add(elt.id)
        elif isinstance(sub, ast.Dict):
            for val in sub.values:
                if isinstance(val, Optional[ast.AST]) and isinstance(val, ast.Name):
                    refs.add(val.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not fnode:
            if sub.decorator_list:
                refs.add(sub.name)
    return refs


def filter_attr_candidates(
    caller_rel: str,
    method_name: str,
    candidates: List[str],
    all_funcs: Dict[str, Dict[str, Any]],
    local_imports: Dict[str, str],
) -> List[str]:
    """过滤未消解属性调用的候选目标，剔除内置容器重名方法与跨文件私有类假边。"""
    if method_name in CONTAINER_METHODS:
        return []
    imported_modules = {
        mod.replace(".", "/") for mod in local_imports.values()
    }
    filtered: List[str] = []
    for cand in candidates:
        cinfo = all_funcs.get(cand)
        if not cinfo:
            continue
        cls_name = cinfo.get("cls")
        if cls_name is None:
            continue
        cand_rel = str(cinfo.get("rel") or "")
        if str(cls_name).startswith("_") and cand_rel != caller_rel:
            cand_stem = cand_rel[:-3] if cand_rel.endswith(".py") else cand_rel
            if cls_name not in local_imports and not any(
                imp == cand_stem or imp.startswith(cand_stem + "/") for imp in imported_modules
            ):
                continue
        filtered.append(cand)
    return filtered


class TypeResolver:
    """全仓轻量级 AST 类型索引与接收者消解器。"""

    def __init__(
        self,
        trees: Dict[str, ast.Module],
        all_funcs: Dict[str, Dict[str, Any]],
        file_imports: Dict[str, Dict[str, str]],
    ) -> None:
        self.trees = trees
        self.all_funcs = all_funcs
        self.file_imports = {rel: dict(imps) for rel, imps in file_imports.items()}
        self._collect_type_checking_imports()
        self.classes_by_name: Dict[str, List[str]] = defaultdict(list)
        self.class_bases: Dict[str, List[str]] = defaultdict(list)
        self.class_methods: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.class_attr_types: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.class_attr_names: Dict[str, Set[str]] = defaultdict(set)
        self.func_return_class: Dict[str, str] = {}
        self.func_by_bare_name: Dict[str, List[str]] = defaultdict(list)
        self.top_funcs_by_rel_name: Dict[Tuple[str, str], str] = {}
        self.inferred_param_types: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.property_names: Set[str] = set()
        self._build_class_index()
        self._build_return_and_attr_types()
        self._propagate_interprocedural_types()

    def _collect_type_checking_imports(self) -> None:
        import callgraph_function_index as _findex

        for rel, tree in self.trees.items():
            imps = self.file_imports.setdefault(rel, {})
            for node in tree.body:
                if isinstance(node, ast.If):
                    test_txt = _callsites.receiver_text(node.test).split(".")[-1]
                    if test_txt == "TYPE_CHECKING":
                        for sub in node.body:
                            _findex._remember_import_aliases(imps, sub, rel)

    def _build_class_index(self) -> None:
        raw_bases: Dict[str, Tuple[str, List[str]]] = {}
        for rel, tree in self.trees.items():
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                ckey = f"{rel}::{node.name}"
                self.classes_by_name[node.name].append(ckey)
                base_names = [
                    b for b in (_unwrap_annotation(base) for base in node.bases) if b
                ]
                raw_bases[ckey] = (rel, base_names)
        for qual, info in self.all_funcs.items():
            if not info.get("nested"):
                self.func_by_bare_name[info["name"]].append(qual)
                if not info.get("cls"):
                    self.top_funcs_by_rel_name[(info["rel"], info["name"])] = qual
            if info.get("cls") and not info.get("nested"):
                ckey = f"{info['rel']}::{info['cls']}"
                self.class_methods[ckey][info["name"]] = qual
                if _is_property_node(info["node"]):
                    self.property_names.add(info["name"])
        for ckey, (rel, base_names) in raw_bases.items():
            for bname in base_names:
                resolved = self.resolve_class_name(rel, bname)
                if resolved:
                    self.class_bases[ckey].append(resolved)

    def _is_subclass(self, child: str, parent: str) -> bool:
        if child == parent:
            return True
        visited: Set[str] = set()
        queue = list(self.class_bases.get(child, []))
        while queue:
            cur = queue.pop(0)
            if cur == parent:
                return True
            if cur in visited:
                continue
            visited.add(cur)
            queue.extend(self.class_bases.get(cur, []))
        return False

    def _common_class(self, classes: Set[str]) -> Optional[str]:
        non_builtin = [c for c in classes if c and c != BUILTIN_TYPE_TAG]
        if not non_builtin:
            return BUILTIN_TYPE_TAG if BUILTIN_TYPE_TAG in classes else None
        if len(non_builtin) == 1:
            return non_builtin[0]
        for candidate in non_builtin:
            if all(self._is_subclass(other, candidate) for other in non_builtin):
                return candidate
        return None

    def resolve_class_name(
        self,
        rel: str,
        raw_name: str,
        local_imports: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        if not raw_name:
            return None
        clean = raw_name.strip()
        if clean.endswith("()"):
            clean = clean[:-2]
        local_ckey = f"{rel}::{clean}"
        if local_ckey in self.class_methods or local_ckey in self.class_bases:
            return local_ckey
        imports = local_imports if local_imports is not None else self.file_imports.get(rel, {})
        if "." in clean:
            head, _, tail = clean.rpartition(".")
            mod = imports.get(head, head)
            cand = f"{mod.replace('.', '/')}.py::{tail}"
            if cand in self.class_methods or cand in self.class_bases:
                return cand
            cand_init = f"{mod.replace('.', '/')}/__init__.py::{tail}"
            if cand_init in self.class_methods or cand_init in self.class_bases:
                return cand_init
            clean = tail
        imported = imports.get(clean)
        if imported and "." in imported:
            mod, _, cls_name = imported.rpartition(".")
            for suffix in (".py", "/__init__.py"):
                cand = f"{mod.replace('.', '/')}{suffix}::{cls_name}"
                if cand in self.class_methods or cand in self.class_bases:
                    return cand
        cands = self.classes_by_name.get(clean, [])
        if len(cands) == 1:
            return cands[0]
        return None

    def _resolve_type_or_builtin(
        self,
        rel: str,
        type_name: str,
        local_imports: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        if not type_name:
            return None
        rcls = self.resolve_class_name(rel, type_name, local_imports)
        if rcls:
            return rcls
        short = type_name.split(".")[-1]
        if short in BUILTIN_ANNOTATIONS or short in BUILTIN_CONSTRUCTORS:
            return BUILTIN_TYPE_TAG
        imports = local_imports if local_imports is not None else self.file_imports.get(rel, {})
        head = type_name.split(".")[0]
        if head in imports:
            mod = imports[head].split(".")[0]
            if mod not in ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop"):
                return BUILTIN_TYPE_TAG
        return None

    def _build_return_and_attr_types(self) -> None:
        for rel, tree in self.trees.items():
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    ckey = f"{rel}::{node.name}"
                    for item in node.body:
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                            self.class_attr_names[ckey].add(item.target.id)
                            fac = _extract_dataclass_default_factory(item.value)
                            rcls = self.resolve_class_name(rel, fac) if fac else None
                            if not rcls:
                                tname = _unwrap_annotation(item.annotation)
                                rcls = self._resolve_type_or_builtin(rel, tname)
                            if not rcls and fac:
                                rcls = self._resolve_type_or_builtin(rel, fac)
                            if rcls:
                                self.class_attr_types[ckey][item.target.id] = rcls
                        elif isinstance(item, ast.Assign):
                            fac = _extract_dataclass_default_factory(item.value)
                            rcls = self._resolve_type_or_builtin(rel, fac) if fac else None
                            for tgt in item.targets:
                                if isinstance(tgt, ast.Name):
                                    self.class_attr_names[ckey].add(tgt.id)
                                    if rcls:
                                        self.class_attr_types[ckey][tgt.id] = rcls

        self._scan_returns_and_self_attrs()

    def _scan_returns_and_self_attrs(self) -> None:
        for qual, info in self.all_funcs.items():
            fnode = info["node"]
            if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rel = info["rel"]
            current_cls = f"{rel}::{info['cls']}" if info.get("cls") else None
            ret_name = _unwrap_annotation(fnode.returns)
            rcls = self._resolve_type_or_builtin(rel, ret_name)
            if rcls:
                self.func_return_class[qual] = rcls
            elif fnode.returns is None and qual not in self.func_return_class:
                param_types = self._param_class_map(rel, fnode, None, qual)
                ret_classes: Set[str] = set()
                for sub in _callsites.walk_runtime_body(fnode):
                    if isinstance(sub, ast.Return) and sub.value is not None:
                        rc = self._expr_class(rel, current_cls, sub.value, param_types)
                        if rc:
                            ret_classes.add(rc)
                common = self._common_class(ret_classes)
                if common:
                    self.func_return_class[qual] = common

        for qual, info in self.all_funcs.items():
            if not info.get("cls") or info.get("nested"):
                continue
            fnode = info["node"]
            if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rel = info["rel"]
            ckey = f"{rel}::{info['cls']}"
            param_types = self._param_class_map(rel, fnode, None, qual)
            for sub in _callsites.walk_runtime_body(fnode):
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Attribute):
                    if isinstance(sub.target.value, ast.Name) and sub.target.value.id == "self":
                        self.class_attr_names[ckey].add(sub.target.attr)
                        rcls = self._resolve_type_or_builtin(rel, _unwrap_annotation(sub.annotation))
                        if not rcls and sub.value is not None:
                            rcls = self._expr_class(rel, ckey, sub.value, param_types)
                        if rcls:
                            self.class_attr_types[ckey][sub.target.attr] = rcls
                elif isinstance(sub, ast.Assign) and sub.value is not None:
                    rcls = self._expr_class(rel, ckey, sub.value, param_types)
                    for tgt in sub.targets:
                        if (
                            isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"
                        ):
                            self.class_attr_names[ckey].add(tgt.attr)
                            if rcls:
                                self.class_attr_types[ckey].setdefault(tgt.attr, rcls)

    def _OrderedParamNames(self, fnode: ast.AST, is_method: bool) -> Tuple[List[str], Set[str]]:
        if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return [], set()
        pos_args = [a.arg for a in list(getattr(fnode.args, "posonlyargs", [])) + list(fnode.args.args)]
        if is_method and pos_args and pos_args[0] in ("self", "cls"):
            pos_args = pos_args[1:]
        kw_names = {a.arg for a in fnode.args.kwonlyargs} | set(pos_args)
        return pos_args, kw_names

    def _resolve_call_target_qual(
        self,
        rel: str,
        current_cls: Optional[str],
        call_node: ast.Call,
        env: Dict[str, str],
        local_imports: Dict[str, str],
    ) -> Tuple[Optional[str], bool]:
        """返回 (target_qual, is_bound_or_init_call)。"""
        func = call_node.func
        if isinstance(func, ast.Name):
            name = func.id
            rcls = self.resolve_class_name(rel, name, local_imports)
            if rcls:
                init_q = self.lookup_method(rcls, "__init__")
                return init_q, True
            local_q = self.top_funcs_by_rel_name.get((rel, name))
            if local_q:
                return local_q, False
            imported = local_imports.get(name)
            if imported and "." in imported:
                mod, _, attr = imported.rpartition(".")
                for suffix in (".py", "/__init__.py"):
                    cand = f"{mod.replace('.', '/')}{suffix}::{attr}"
                    if cand in self.all_funcs:
                        return cand, False
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id in local_imports:
                mod = local_imports[func.value.id]
                for suffix in (".py", "/__init__.py"):
                    cand = f"{mod.replace('.', '/')}{suffix}::{func.attr}"
                    if cand in self.all_funcs:
                        return cand, False
            recv = _callsites.receiver_text(func.value)
            rcls = self.resolve_receiver_class(recv, rel, current_cls, env, local_imports)
            if rcls and rcls != BUILTIN_TYPE_TAG:
                mqual = self.lookup_method(rcls, func.attr)
                if mqual:
                    return mqual, True
        return None, False

    def _propagate_interprocedural_types(self) -> None:
        """2 轮不动点跨函数调用点实参类型传播：将已知类型的实参自动传播到目标函数未注解形参。"""
        for _round in range(2):
            collected: Dict[str, Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
            for qual, info in self.all_funcs.items():
                fnode = info["node"]
                rel = info["rel"]
                current_cls = f"{rel}::{info['cls']}" if info.get("cls") else None
                local_imports = dict(self.file_imports.get(rel, {}))
                env = self.infer_function_env(info, local_imports, qual)
                for sub in _callsites.walk_runtime_body(fnode):
                    if not isinstance(sub, ast.Call):
                        continue
                    target_qual, is_method = self._resolve_call_target_qual(
                        rel, current_cls, sub, env, local_imports
                    )
                    if not target_qual or target_qual not in self.all_funcs:
                        continue
                    tinfo = self.all_funcs[target_qual]
                    pos_params, all_param_names = self._OrderedParamNames(
                        tinfo["node"], is_method or bool(tinfo.get("cls"))
                    )
                    for idx, arg_expr in enumerate(sub.args):
                        if idx >= len(pos_params):
                            break
                        acls = self._expr_class(rel, current_cls, arg_expr, env, local_imports)
                        if acls and acls != BUILTIN_TYPE_TAG:
                            collected[target_qual][pos_params[idx]].add(acls)
                    for kw in sub.keywords:
                        if kw.arg and kw.arg in all_param_names:
                            acls = self._expr_class(rel, current_cls, kw.value, env, local_imports)
                            if acls and acls != BUILTIN_TYPE_TAG:
                                collected[target_qual][kw.arg].add(acls)
            changed = False
            for tqual, pmap in collected.items():
                for pname, cset in pmap.items():
                    common = self._common_class(cset)
                    if common and common != BUILTIN_TYPE_TAG:
                        if self.inferred_param_types[tqual].get(pname) != common:
                            self.inferred_param_types[tqual][pname] = common
                            changed = True
            if not changed:
                break
            self._scan_returns_and_self_attrs()

    def _param_class_map(
        self,
        rel: str,
        fnode: ast.AST,
        local_imports: Optional[Dict[str, str]] = None,
        qual: Optional[str] = None,
    ) -> Dict[str, str]:
        if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return {}
        args = list(getattr(fnode.args, "posonlyargs", [])) + list(fnode.args.args) + list(fnode.args.kwonlyargs)
        out: Dict[str, str] = {}
        inferred = self.inferred_param_types.get(qual, {}) if qual else {}
        for arg in args:
            if arg.arg in ("self", "cls"):
                continue
            if arg.annotation is not None:
                ann_name = _unwrap_annotation(arg.annotation)
                rcls = self._resolve_type_or_builtin(rel, ann_name, local_imports)
                if rcls:
                    out[arg.arg] = rcls
                    continue
            if arg.arg in inferred:
                out[arg.arg] = inferred[arg.arg]
        return out

    def _expr_class(
        self,
        rel: str,
        current_cls: Optional[str],
        expr: ast.AST,
        env: Dict[str, str],
        local_imports: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        if isinstance(
            expr,
            (ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.DictComp, ast.SetComp),
        ):
            return BUILTIN_TYPE_TAG
        if isinstance(expr, ast.Constant) and isinstance(expr.value, (str, bytes, int, float, bool)):
            return BUILTIN_TYPE_TAG
        if isinstance(expr, ast.Name):
            return env.get(expr.id)
        if isinstance(expr, ast.Subscript):
            vcls = self._expr_class(rel, current_cls, expr.value, env, local_imports)
            if vcls == BUILTIN_TYPE_TAG:
                return BUILTIN_TYPE_TAG
            return None
        if isinstance(expr, ast.IfExp):
            left = self._expr_class(rel, current_cls, expr.body, env, local_imports)
            if left and left != BUILTIN_TYPE_TAG:
                return left
            right = self._expr_class(rel, current_cls, expr.orelse, env, local_imports)
            return right or left
        if isinstance(expr, ast.BoolOp):
            for val in expr.values:
                rc = self._expr_class(rel, current_cls, val, env, local_imports)
                if rc and rc != BUILTIN_TYPE_TAG:
                    return rc
            return None
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id == "self" and current_cls:
                return self._lookup_class_attr(current_cls, expr.attr)
            vcls = self._expr_class(rel, current_cls, expr.value, env, local_imports)
            if vcls == BUILTIN_TYPE_TAG:
                return BUILTIN_TYPE_TAG
            if vcls:
                attr_cls = self._lookup_class_attr(vcls, expr.attr)
                if attr_cls:
                    return attr_cls
                mqual = self.lookup_method(vcls, expr.attr)
                if mqual and mqual in self.func_return_class:
                    return self.func_return_class[mqual]
        if isinstance(expr, ast.Call):
            callee = _callsites.receiver_text(expr.func)
            if callee in BUILTIN_CONSTRUCTORS or callee.split(".")[-1] in BUILTIN_CONSTRUCTORS:
                return BUILTIN_TYPE_TAG
            rcls = self.resolve_class_name(rel, callee, local_imports)
            if rcls:
                return rcls
            if callee in ("cls", "self.__class__") and current_cls:
                return current_cls
            if isinstance(expr.func, ast.Name):
                fname = expr.func.id
                local_q = self.top_funcs_by_rel_name.get((rel, fname))
                if local_q and local_q in self.func_return_class:
                    return self.func_return_class[local_q]
                cands = self.func_by_bare_name.get(fname, [])
                if len(cands) == 1 and cands[0] in self.func_return_class:
                    return self.func_return_class[cands[0]]
                imports = local_imports if local_imports is not None else self.file_imports.get(rel, {})
                if fname in imports:
                    mod = imports[fname].split(".")[0]
                    if mod not in ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop"):
                        return BUILTIN_TYPE_TAG
            elif isinstance(expr.func, ast.Attribute):
                recv_cls = self._expr_class(rel, current_cls, expr.func.value, env, local_imports)
                if recv_cls == BUILTIN_TYPE_TAG:
                    return BUILTIN_TYPE_TAG
                if recv_cls:
                    mqual = self.lookup_method(recv_cls, expr.func.attr)
                    if mqual and mqual in self.func_return_class:
                        return self.func_return_class[mqual]
                imports = local_imports if local_imports is not None else self.file_imports.get(rel, {})
                head = callee.split(".")[0]
                if head in imports:
                    mod = imports[head].split(".")[0]
                    if mod not in ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop"):
                        return BUILTIN_TYPE_TAG
        return None

    def _lookup_class_attr(self, ckey: str, attr: str) -> Optional[str]:
        visited: Set[str] = set()
        queue = [ckey]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            if attr in self.class_attr_types.get(cur, {}):
                return self.class_attr_types[cur][attr]
            queue.extend(self.class_bases.get(cur, []))
        return None

    def _has_class_attr(self, ckey: str, attr: str) -> bool:
        visited: Set[str] = set()
        queue = [ckey]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            if attr in self.class_attr_names.get(cur, set()):
                return True
            queue.extend(self.class_bases.get(cur, []))
        return False

    def lookup_method(self, ckey: str, method_name: str, *, skip_self: bool = False) -> Optional[str]:
        """沿继承链（BFS）查找类及其第一方基类上的方法定义。"""
        visited: Set[str] = set()
        queue = list(self.class_bases.get(ckey, [])) if skip_self else [ckey]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            methods = self.class_methods.get(cur, {})
            if method_name in methods:
                return methods[method_name]
            queue.extend(self.class_bases.get(cur, []))
        return None

    def infer_function_env(
        self,
        info: Dict[str, Any],
        local_imports: Dict[str, str],
        qual: Optional[str] = None,
    ) -> Dict[str, str]:
        fnode = info["node"]
        rel = info["rel"]
        q = qual or str(info.get("qual") or "")
        current_cls = f"{rel}::{info['cls']}" if info.get("cls") else None
        env = self._param_class_map(rel, fnode, local_imports, q)
        parent_qual = info.get("parent")
        while parent_qual and parent_qual in self.all_funcs:
            pinfo = self.all_funcs[parent_qual]
            penv = self._param_class_map(rel, pinfo["node"], local_imports, parent_qual)
            for k, v in penv.items():
                env.setdefault(k, v)
            parent_qual = pinfo.get("parent")
        for sub in _callsites.walk_runtime_body(fnode):
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                ann_name = _unwrap_annotation(sub.annotation)
                rcls = self._resolve_type_or_builtin(rel, ann_name, local_imports)
                if not rcls and sub.value is not None:
                    rcls = self._expr_class(rel, current_cls, sub.value, env, local_imports)
                if rcls:
                    env[sub.target.id] = rcls
            elif isinstance(sub, ast.Assign) and sub.value is not None:
                rcls = self._expr_class(rel, current_cls, sub.value, env, local_imports)
                if rcls:
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name):
                            env[tgt.id] = rcls
            elif isinstance(sub, (ast.With, ast.AsyncWith)):
                for item in sub.items:
                    if isinstance(item.optional_vars, ast.Name):
                        rcls = self._expr_class(rel, current_cls, item.context_expr, env, local_imports)
                        if rcls:
                            env[item.optional_vars.id] = rcls
        return env

    def resolve_receiver_class(
        self,
        receiver: str,
        rel: str,
        current_cls: Optional[str],
        env: Dict[str, str],
        local_imports: Dict[str, str],
    ) -> Optional[str]:
        if receiver in ("self", "cls") and current_cls:
            return current_cls
        if receiver in env:
            return env[receiver]
        if receiver.endswith("[]"):
            base_cls = self.resolve_receiver_class(receiver[:-2], rel, current_cls, env, local_imports)
            if base_cls == BUILTIN_TYPE_TAG:
                return BUILTIN_TYPE_TAG
        if receiver.endswith("()"):
            callee = receiver[:-2]
            local_q = self.top_funcs_by_rel_name.get((rel, callee))
            if local_q and local_q in self.func_return_class:
                return self.func_return_class[local_q]
            return self.resolve_class_name(rel, callee, local_imports)
        if receiver.startswith("self.") and current_cls:
            parts = receiver.split(".")[1:]
            cur: Optional[str] = current_cls
            for part in parts:
                if not cur or cur == BUILTIN_TYPE_TAG:
                    return cur
                if part.endswith("()"):
                    mqual = self.lookup_method(cur, part[:-2])
                    cur = self.func_return_class.get(mqual, None) if mqual else None
                else:
                    attr_cls = self._lookup_class_attr(cur, part)
                    if not attr_cls:
                        mqual = self.lookup_method(cur, part)
                        attr_cls = self.func_return_class.get(mqual, None) if mqual else None
                    cur = attr_cls
            return cur
        if "." in receiver:
            head, _, tail = receiver.partition(".")
            cur = env.get(head)
            if cur:
                for part in tail.split("."):
                    if not cur or cur == BUILTIN_TYPE_TAG:
                        return cur
                    if part.endswith("()"):
                        mqual = self.lookup_method(cur, part[:-2])
                        cur = self.func_return_class.get(mqual, None) if mqual else None
                    else:
                        attr_cls = self._lookup_class_attr(cur, part)
                        if not attr_cls:
                            mqual = self.lookup_method(cur, part)
                            attr_cls = self.func_return_class.get(mqual, None) if mqual else None
                        cur = attr_cls
                return cur
            return self.resolve_class_name(rel, receiver, local_imports)
        return self.resolve_class_name(rel, receiver, local_imports)

    def _collect_property_reads(
        self,
        qual: str,
        rel: str,
        current_cls: Optional[str],
        fnode: ast.AST,
        env: Dict[str, str],
        local_imports: Dict[str, str],
    ) -> List[Edge]:
        if not self.property_names:
            return []
        call_funcs: Set[int] = set()
        prop_reads: Set[Tuple[str, str]] = set()
        for sub in _callsites.walk_runtime_body(fnode):
            if isinstance(sub, ast.Call):
                call_funcs.add(id(sub.func))
            elif (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.ctx, ast.Load)
                and id(sub) not in call_funcs
                and sub.attr in self.property_names
            ):
                recv = _callsites.receiver_text(sub.value)
                if recv:
                    prop_reads.add((recv, sub.attr))
        edges: List[Edge] = []
        for recv, prop_name in sorted(prop_reads):
            rcls = self.resolve_receiver_class(recv, rel, current_cls, env, local_imports)
            if not rcls or rcls == BUILTIN_TYPE_TAG:
                continue
            target_qual = self.lookup_method(rcls, prop_name)
            if target_qual and target_qual != qual:
                edges.append({"from": qual, "to": target_qual, "kind": "typed", "ambiguous": False})
        return edges

    def resolve_attr_calls(
        self,
        qual: str,
        info: Dict[str, Any],
        calls: Set[AttrCall],
        local_imports: Dict[str, str],
    ) -> Tuple[List[Edge], Set[AttrCall]]:
        """Tier 1：消解已知接收者类型的属性方法调用与 @property 访问，并过滤内置容器/外部模块调用。"""
        rel = info["rel"]
        merged_imports = dict(self.file_imports.get(rel, {}))
        merged_imports.update(local_imports)
        local_imports = merged_imports
        current_cls = f"{rel}::{info['cls']}" if info.get("cls") else None
        env = self.infer_function_env(info, local_imports, qual)
        typed_edges: List[Edge] = self._collect_property_reads(
            qual, rel, current_cls, info["node"], env, local_imports
        )
        resolved: Set[AttrCall] = set()
        for call in sorted(calls):
            receiver, method_name, _line = call
            if receiver == "super()" and current_cls:
                target_qual = self.lookup_method(current_cls, method_name, skip_self=True)
                if target_qual and target_qual != qual:
                    typed_edges.append({"from": qual, "to": target_qual, "kind": "typed", "ambiguous": False})
                resolved.add(call)
                continue
            if receiver == "self" and current_cls:
                target_qual = self.lookup_method(current_cls, method_name)
                if target_qual and target_qual != qual:
                    if not target_qual.startswith(f"{current_cls}."):
                        typed_edges.append({"from": qual, "to": target_qual, "kind": "typed", "ambiguous": False})
                        resolved.add(call)
                    continue
                if self._has_class_attr(current_cls, method_name):
                    # self.clock() 等调用实例上的可调用属性字段，不是外部同名类方法
                    resolved.add(call)
                    continue
            base_head = receiver.split(".")[0].rstrip("()")
            if base_head in local_imports and not self.resolve_class_name(rel, base_head, local_imports):
                resolved.add(call)
                continue
            if (
                receiver.endswith(("[]", ".setdefault()", ".get()", ".values()", ".keys()", ".items()", ".splitlines()", ".split()", ".strip()"))
                and method_name in CONTAINER_METHODS
            ):
                resolved.add(call)
                continue
            rcls = self.resolve_receiver_class(receiver, rel, current_cls, env, local_imports)
            if rcls == BUILTIN_TYPE_TAG:
                resolved.add(call)
                continue
            if not rcls:
                continue
            target_qual = self.lookup_method(rcls, method_name)
            if target_qual and target_qual != qual:
                typed_edges.append({"from": qual, "to": target_qual, "kind": "typed", "ambiguous": False})
                resolved.add(call)
            elif self._has_class_attr(rcls, method_name):
                resolved.add(call)
        return typed_edges, resolved


# ---------------------------------------------------------------------------
# Tier 2（重型档）：融合 SCIP 索引 + sys.setprofile 运行时调用边
# ---------------------------------------------------------------------------

def apply_deep_resolution(
    repo_root: str,
    edges: List[Edge],
    all_funcs: Dict[str, Dict[str, Any]],
    *,
    scip_index_path: Optional[str] = None,
    runtime_edges_path: Optional[str] = None,
) -> Tuple[List[Edge], Dict[str, int]]:
    """Tier 2：用 SCIP 索引和/或运行时调用边将模糊边升级为确信边，并剔除同调用点假候选。"""
    stats = {
        "scip_promoted": 0,
        "scip_pruned_false_candidates": 0,
        "runtime_promoted": 0,
        "runtime_added_dynamic": 0,
    }
    confirmed_pairs: Set[Tuple[str, str]] = set()
    confirmed_attr_names: Set[Tuple[str, str]] = set()

    if scip_index_path and os.path.isfile(scip_index_path):
        scip_pairs, scip_external_attrs = _extract_scip_call_pairs(repo_root, scip_index_path, all_funcs)
        for src, dst in scip_pairs:
            confirmed_pairs.add((src, dst))
            method_name = dst.split("::")[-1].split(".")[-1]
            confirmed_attr_names.add((src, method_name))
        confirmed_attr_names.update(scip_external_attrs)

    runtime_pairs: Set[Tuple[str, str]] = set()
    if runtime_edges_path and os.path.isfile(runtime_edges_path):
        with open(runtime_edges_path, encoding="utf-8") as fh:
            raw_rt = json.load(fh)
        for item in raw_rt:
            src, dst = str(item.get("from") or ""), str(item.get("to") or "")
            if src in all_funcs and dst in all_funcs and src != dst:
                runtime_pairs.add((src, dst))
                method_name = dst.split("::")[-1].split(".")[-1]
                confirmed_attr_names.add((src, method_name))

    if not confirmed_pairs and not runtime_pairs:
        return edges, stats

    existing_confident: Set[Tuple[str, str]] = {
        (e["from"], e["to"]) for e in edges if not e["ambiguous"]
    }
    new_edges: List[Edge] = []
    for edge in edges:
        pair = (edge["from"], edge["to"])
        if not edge["ambiguous"]:
            new_edges.append(edge)
            continue
        if pair in confirmed_pairs:
            new_edges.append({"from": edge["from"], "to": edge["to"], "kind": "scip", "ambiguous": False})
            existing_confident.add(pair)
            stats["scip_promoted"] += 1
        elif pair in runtime_pairs:
            new_edges.append({"from": edge["from"], "to": edge["to"], "kind": "runtime", "ambiguous": False})
            existing_confident.add(pair)
            stats["runtime_promoted"] += 1
        elif edge["kind"] == "attr":
            mname = edge["to"].split("::")[-1].split(".")[-1]
            if (edge["from"], mname) in confirmed_attr_names:
                stats["scip_pruned_false_candidates"] += 1
                continue
            new_edges.append(edge)
        else:
            new_edges.append(edge)

    for pair in sorted(runtime_pairs - existing_confident):
        new_edges.append({"from": pair[0], "to": pair[1], "kind": "runtime", "ambiguous": False})
        stats["runtime_added_dynamic"] += 1

    return new_edges, stats


def _extract_scip_call_pairs(
    repo_root: str,
    scip_index_path: str,
    all_funcs: Dict[str, Dict[str, Any]],
) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    import shutil
    import subprocess

    scip_bin = shutil.which("scip") or os.path.expanduser("~/.local/bin/scip")
    if not os.path.isfile(scip_bin):
        return set(), set()
    proc = subprocess.run(
        [scip_bin, "print", "--json", scip_index_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return set(), set()
    try:
        payload = json.loads(proc.stdout)
        docs = payload.get("documents", []) if isinstance(payload, dict) else payload
    except ValueError:
        return set(), set()

    funcs_by_rel_name: Dict[Tuple[str, str], List[Tuple[int, int, str]]] = defaultdict(list)
    funcs_by_rel: Dict[str, List[Tuple[int, int, str]]] = defaultdict(list)
    for qual, info in all_funcs.items():
        rel = info["rel"]
        lo, hi = int(info["line"]), int(info["end"])
        funcs_by_rel_name[(rel, info["name"])].append((lo, hi, qual))
        funcs_by_rel[rel].append((lo, hi, qual))

    sym_to_qual: Dict[str, str] = {}
    doc_calibrated_spans: Dict[str, List[Tuple[int, int, str]]] = {}
    method_sym_re = re.compile(r"[/#]([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?\.$")
    for doc in docs:
        rel = str(doc.get("relative_path") or doc.get("path") or "")
        def_lines_by_qual: Dict[str, int] = {}
        for occ in doc.get("occurrences") or []:
            roles = int(occ.get("symbol_roles") or occ.get("symbolRoles") or 0)
            if not (roles & 1):
                continue
            sym = str(occ.get("symbol") or "")
            m = method_sym_re.search(sym)
            if not m:
                continue
            fname = m.group(1)
            cands = funcs_by_rel_name.get((rel, fname), [])
            if not cands:
                continue
            rng = occ.get("range") or []
            scip_line = (int(rng[0]) + 1) if rng else 0
            if len(cands) == 1:
                qual = cands[0][2]
                sym_to_qual[sym] = qual
                if scip_line:
                    def_lines_by_qual[qual] = scip_line
            elif scip_line:
                best = min(cands, key=lambda item: abs(item[0] - scip_line))
                if abs(best[0] - scip_line) <= 40:
                    sym_to_qual[sym] = best[2]
                    def_lines_by_qual[best[2]] = scip_line
        calibrated: List[Tuple[int, int, str]] = []
        for lo, hi, qual in funcs_by_rel.get(rel, []):
            scip_lo = def_lines_by_qual.get(qual, lo)
            delta = scip_lo - lo
            calibrated.append((lo + delta, hi + delta, qual))
        doc_calibrated_spans[rel] = calibrated

    pairs: Set[Tuple[str, str]] = set()
    external_attrs: Set[Tuple[str, str]] = set()
    for doc in docs:
        rel = str(doc.get("relative_path") or doc.get("path") or "")
        spans = doc_calibrated_spans.get(rel) or funcs_by_rel.get(rel, [])
        if not spans:
            continue
        for occ in doc.get("occurrences") or []:
            roles = int(occ.get("symbol_roles") or occ.get("symbolRoles") or 0)
            if roles & 1:
                continue
            sym = str(occ.get("symbol") or "")
            if sym.startswith("local "):
                continue
            rng = occ.get("range") or []
            if not rng:
                continue
            line = int(rng[0]) + 1
            containing = [(hi - lo, q) for lo, hi, q in spans if lo <= line <= hi]
            if not containing:
                continue
            caller_qual = min(containing)[1]
            target_qual = sym_to_qual.get(sym)
            if target_qual and target_qual != caller_qual:
                pairs.add((caller_qual, target_qual))
            elif "#" in sym:
                m = method_sym_re.search(sym)
                if m:
                    external_attrs.add((caller_qual, m.group(1)))
    return pairs, external_attrs
