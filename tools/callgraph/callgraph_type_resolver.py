"""调用图两档消歧器：轻量 AST 类型推断（Tier 1） + 重型 SCIP / Runtime 融合（Tier 2）。

- Tier 1（默认开启，0 外部依赖，毫秒级）：
  利用类继承表、函数参数/返回值类型注解、类属性注解与 __init__ 赋值（self.x = Cls()）、
  局部变量构造/注解/with 绑定，将 ``obj.method()`` 与 ``self.attr.method()`` 消解为
  ``kind="typed", ambiguous=False`` 的确信边，同时剔除向全仓其他同名类乱连的假模糊边。
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
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import callgraph_call_sites as _callsites

AttrCall = Tuple[str, str, int]
Edge = Dict[str, Any]

BUILTIN_TYPE_TAG = "__builtin__"
BUILTIN_CONSTRUCTORS = frozenset({
    "list", "dict", "set", "frozenset", "tuple", "str", "bytes", "bytearray",
    "int", "float", "bool", "defaultdict", "Counter", "deque", "OrderedDict",
    "Path", "PurePath", "PosixPath", "WindowsPath", "datetime", "date", "time", "timedelta",
    "re.compile", "io.StringIO", "io.BytesIO", "StringIO", "BytesIO",
})
BUILTIN_ANNOTATIONS = frozenset({
    "list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator",
    "dict", "Dict", "Mapping", "MutableMapping", "Counter", "defaultdict", "OrderedDict",
    "set", "Set", "AbstractSet", "MutableSet", "frozenset", " FrozenSet",
    "tuple", "Tuple", "str", "bytes", "bytearray", "int", "float", "bool",
    "Path", "PurePath", "datetime", "date", "time", "timedelta", "Pattern",
})
CONTAINER_METHODS = frozenset({
    "append", "extend", "insert", "pop", "remove", "clear", "copy", "sort", "reverse",
    "get", "keys", "values", "items", "setdefault", "update", "popitem",
    "add", "discard", "union", "intersection", "difference",
    "strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "join",
    "replace", " lower", "upper", "startswith", "endswith", "find", "rfind",
    "encode", "decode", "format", "isascii", "isdigit", "isdecimal", "isalnum", "isalpha",
    "exists", "is_file", "is_dir", "read_text", "write_text", "read_bytes", "write_bytes",
    "mkdir", "unlink", "resolve", "glob", "rglob", "with_name", "with_suffix",
    "isoformat", "fromisoformat", "total_seconds", "strftime", "strptime",
})


def _unwrap_annotation(node: Optional[ast.AST]) -> str:
    """从类型注解 AST 中提取核心类名（剥离 Optional / Union / Annotated / 字符串引号）。"""
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value.strip()
        if "[" in text:
            head, _, inner = text.partition("[")
            if head.split(".")[-1] in ("Optional", "Union", "Annotated", "Final", "ClassVar"):
                first = inner.rstrip("]").split(",")[0].strip()
                return first
        return text
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _unwrap_annotation(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        wrapper = _unwrap_annotation(node.value).split(".")[-1]
        if wrapper in ("Optional", "Union", "Annotated", "Final", "ClassVar"):
            slc = node.slice
            if isinstance(slc, ast.Tuple) and slc.elts:
                for elt in slc.elts:
                    name = _unwrap_annotation(elt)
                    if name and name != "None":
                        return name
            return _unwrap_annotation(slc)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _unwrap_annotation(node.left)
        return left if left and left != "None" else _unwrap_annotation(node.right)
    return ""


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
        self.file_imports = file_imports
        # class_key = "rel::ClassName"
        self.classes_by_name: Dict[str, List[str]] = defaultdict(list)
        self.class_bases: Dict[str, List[str]] = defaultdict(list)
        self.class_methods: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.class_attr_types: Dict[str, Dict[str, str]] = defaultdict(dict)
        self.func_return_class: Dict[str, str] = {}
        self.func_by_bare_name: Dict[str, List[str]] = defaultdict(list)
        self._build_class_index()
        self._build_return_and_attr_types()

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
            if info.get("cls") and not info.get("nested"):
                ckey = f"{info['rel']}::{info['cls']}"
                self.class_methods[ckey][info["name"]] = qual
        for ckey, (rel, base_names) in raw_bases.items():
            for bname in base_names:
                resolved = self.resolve_class_name(rel, bname)
                if resolved:
                    self.class_bases[ckey].append(resolved)

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

    def _build_return_and_attr_types(self) -> None:
        # 1) 函数返回类型注解 + 类体属性类型注解
        for rel, tree in self.trees.items():
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    ckey = f"{rel}::{node.name}"
                    for item in node.body:
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                            tname = _unwrap_annotation(item.annotation)
                            rcls = self.resolve_class_name(rel, tname)
                            if rcls:
                                self.class_attr_types[ckey][item.target.id] = rcls
        for qual, info in self.all_funcs.items():
            fnode = info["node"]
            if isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ret_name = _unwrap_annotation(fnode.returns)
                rcls = self.resolve_class_name(info["rel"], ret_name)
                if rcls:
                    self.func_return_class[qual] = rcls
                # 若无返回注解，检查是否所有 return 均直接返回同一个类构造 Cls(...)
                elif fnode.returns is None:
                    ret_classes: Set[str] = set()
                    for sub in _callsites.walk_runtime_body(fnode):
                        if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call):
                            cname = _callsites.receiver_text(sub.value.func)
                            rc = self.resolve_class_name(info["rel"], cname)
                            if rc:
                                ret_classes.add(rc)
                    if len(ret_classes) == 1:
                        self.func_return_class[qual] = next(iter(ret_classes))

        # 2) 扫描每个类的 __init__ 等方法，推导 self.attr = Cls(...) 或 self.attr = param
        for qual, info in self.all_funcs.items():
            if not info.get("cls") or info.get("nested"):
                continue
            fnode = info["node"]
            if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rel = info["rel"]
            ckey = f"{rel}::{info['cls']}"
            param_types = self._param_class_map(rel, fnode)
            for sub in _callsites.walk_runtime_body(fnode):
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Attribute):
                    if isinstance(sub.target.value, ast.Name) and sub.target.value.id == "self":
                        rcls = self.resolve_class_name(rel, _unwrap_annotation(sub.annotation))
                        if rcls:
                            self.class_attr_types[ckey][sub.target.attr] = rcls
                elif isinstance(sub, ast.Assign) and sub.value is not None:
                    rcls = self._expr_class(rel, ckey, sub.value, param_types)
                    if not rcls:
                        continue
                    for tgt in sub.targets:
                        if (
                            isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"
                        ):
                            self.class_attr_types[ckey].setdefault(tgt.attr, rcls)

    def _param_class_map(
        self,
        rel: str,
        fnode: ast.AST,
        local_imports: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return {}
        args = list(getattr(fnode.args, "posonlyargs", [])) + list(fnode.args.args) + list(fnode.args.kwonlyargs)
        out: Dict[str, str] = {}
        for arg in args:
            if arg.arg in ("self", "cls") or arg.annotation is None:
                continue
            ann_name = _unwrap_annotation(arg.annotation)
            rcls = self.resolve_class_name(rel, ann_name, local_imports)
            if rcls:
                out[arg.arg] = rcls
            elif ann_name.split(".")[-1] in BUILTIN_ANNOTATIONS:
                out[arg.arg] = BUILTIN_TYPE_TAG
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
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id == "self" and current_cls:
                return self._lookup_class_attr(current_cls, expr.attr)
        if isinstance(expr, ast.Call):
            callee = _callsites.receiver_text(expr.func)
            if callee in BUILTIN_CONSTRUCTORS or callee.split(".")[-1] in BUILTIN_CONSTRUCTORS:
                return BUILTIN_TYPE_TAG
            # 1) 直接类名实例化 Cls(...) 或 mod.Cls(...)
            rcls = self.resolve_class_name(rel, callee, local_imports)
            if rcls:
                return rcls
            # 2) cls(...) / self.__class__(...)
            if callee in ("cls", "self.__class__") and current_cls:
                return current_cls
            # 3) 已知返回类型的函数或方法调用
            if isinstance(expr.func, ast.Name):
                cands = self.func_by_bare_name.get(expr.func.id, [])
                if len(cands) == 1 and cands[0] in self.func_return_class:
                    return self.func_return_class[cands[0]]
            elif isinstance(expr.func, ast.Attribute):
                recv_cls = self._expr_class(rel, current_cls, expr.func.value, env, local_imports)
                if recv_cls:
                    mqual = self.lookup_method(recv_cls, expr.func.attr)
                    if mqual and mqual in self.func_return_class:
                        return self.func_return_class[mqual]
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

    def lookup_method(self, ckey: str, method_name: str) -> Optional[str]:
        """沿继承链（BFS）查找类及其第一方基类上的方法定义。"""
        visited: Set[str] = set()
        queue = [ckey]
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
    ) -> Dict[str, str]:
        fnode = info["node"]
        rel = info["rel"]
        current_cls = f"{rel}::{info['cls']}" if info.get("cls") else None
        env = self._param_class_map(rel, fnode, local_imports)
        for sub in _callsites.walk_runtime_body(fnode):
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                ann_name = _unwrap_annotation(sub.annotation)
                rcls = self.resolve_class_name(rel, ann_name, local_imports)
                if not rcls and ann_name.split(".")[-1] in BUILTIN_ANNOTATIONS:
                    rcls = BUILTIN_TYPE_TAG
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
        if receiver.endswith("()"):
            return self.resolve_class_name(rel, receiver[:-2], local_imports)
        if receiver.startswith("self.") and current_cls:
            parts = receiver.split(".")[1:]
            cur: Optional[str] = current_cls
            for part in parts:
                if not cur:
                    return None
                if part.endswith("()"):
                    mqual = self.lookup_method(cur, part[:-2])
                    cur = self.func_return_class.get(mqual, None) if mqual else None
                else:
                    cur = self._lookup_class_attr(cur, part)
            return cur
        if "." in receiver:
            head, _, tail = receiver.partition(".")
            cur = env.get(head)
            if cur:
                for part in tail.split("."):
                    if not cur:
                        return None
                    cur = self._lookup_class_attr(cur, part)
                return cur
            return self.resolve_class_name(rel, receiver, local_imports)
        return self.resolve_class_name(rel, receiver, local_imports)

    def resolve_attr_calls(
        self,
        qual: str,
        info: Dict[str, Any],
        calls: Set[AttrCall],
        local_imports: Dict[str, str],
    ) -> Tuple[List[Edge], Set[AttrCall]]:
        """Tier 1：消解已知接收者类型的属性方法调用，并过滤内置容器/外部模块调用。"""
        if not calls:
            return [], set()
        rel = info["rel"]
        current_cls = f"{rel}::{info['cls']}" if info.get("cls") else None
        env = self.infer_function_env(info, local_imports)
        typed_edges: List[Edge] = []
        resolved: Set[AttrCall] = set()
        for call in sorted(calls):
            receiver, method_name, _line = call
            # 1) 若接收者本身是已 import 的模块名（且未在第一方 module_import_attr 中命中），属外部库调用
            base_head = receiver.split(".")[0].rstrip("()")
            if base_head in local_imports and not self.resolve_class_name(rel, base_head, local_imports):
                resolved.add(call)
                continue
            # 2) 若接收者是容器下标/内置链式调用（如 d.setdefault()、rows[]）且调用常见容器方法
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
    confirmed_attr_names: Set[Tuple[str, str]] = set()  # (from_qual, method_name)

    # 1) SCIP 索引消歧
    if scip_index_path and os.path.isfile(scip_index_path):
        scip_pairs, scip_external_attrs = _extract_scip_call_pairs(repo_root, scip_index_path, all_funcs)
        for src, dst in scip_pairs:
            confirmed_pairs.add((src, dst))
            method_name = dst.split("::")[-1].split(".")[-1]
            confirmed_attr_names.add((src, method_name))
        confirmed_attr_names.update(scip_external_attrs)

    # 2) Runtime setprofile 动态调用边消歧
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
                # 同一 (caller, method_name) 已被 SCIP/Runtime 确认指向其他真实类，剪除未命中的同名假候选
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

    # 1) 建立 SCIP symbol -> qual 映射（按 rel + 函数名 + 行号区间匹配）
    funcs_by_rel_name: Dict[Tuple[str, str], List[Tuple[int, int, str]]] = defaultdict(list)
    funcs_by_rel: Dict[str, List[Tuple[int, int, str]]] = defaultdict(list)
    for qual, info in all_funcs.items():
        rel = info["rel"]
        lo, hi = int(info["line"]), int(info["end"])
        funcs_by_rel_name[(rel, info["name"])].append((lo, hi, qual))
        funcs_by_rel[rel].append((lo, hi, qual))

    sym_to_qual: Dict[str, str] = {}
    method_sym_re = re.compile(r"[/#]([A-Za-z_][A-Za-z0-9_]*)\(\)\.$")
    for doc in docs:
        rel = str(doc.get("relative_path") or doc.get("path") or "")
        for occ in doc.get("occurrences") or []:
            roles = int(occ.get("symbol_roles") or occ.get("symbolRoles") or 0)
            if not (roles & 1):  # DEFINITION_ROLE = 1
                continue
            sym = str(occ.get("symbol") or "")
            m = method_sym_re.search(sym)
            if not m:
                continue
            fname = m.group(1)
            cands = funcs_by_rel_name.get((rel, fname), [])
            if not cands:
                continue
            if len(cands) == 1:
                sym_to_qual[sym] = cands[0][2]
            else:
                rng = occ.get("range") or []
                line = (int(rng[0]) + 1) if rng else 0
                matched = [q for lo, hi, q in cands if lo - 5 <= line <= hi]
                if len(matched) == 1:
                    sym_to_qual[sym] = matched[0]

    # 2) 遍历非定义引用：第一方目标进 pairs，标准库/第三方已知方法进 external_attrs 剪除假模糊边
    pairs: Set[Tuple[str, str]] = set()
    external_attrs: Set[Tuple[str, str]] = set()
    for doc in docs:
        rel = str(doc.get("relative_path") or doc.get("path") or "")
        spans = funcs_by_rel.get(rel, [])
        if not spans:
            continue
        for occ in doc.get("occurrences") or []:
            roles = int(occ.get("symbol_roles") or occ.get("symbolRoles") or 0)
            if roles & 1:
                continue
            sym = str(occ.get("symbol") or "")
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
            elif "#" in sym and sym.endswith("()."):
                # SCIP 已明确将该属性方法消解到标准库或第三方包（如 builtins.list#append().）
                m = method_sym_re.search(sym)
                if m:
                    external_attrs.add((caller_qual, m.group(1)))
    return pairs, external_attrs
