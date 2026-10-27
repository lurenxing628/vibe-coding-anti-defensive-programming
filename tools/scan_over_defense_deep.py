#!/usr/bin/env python3
"""全量反过度防御 AST / JS 深度机械扫描器（对照反过度防御黑名单 15 母题 / 140 案例中可机械识别的 20+ 类信号）。

只读：解析源码 AST / 文本，结果写到 --out 目录，不改仓库。
"""
import argparse
import ast
import collections
import json
import os
import re
import sys

PROD_DIRS = ("src", "core", "data", "web", "app", "plugins", "desktop")
PROD_ROOT_FILES = ("app.py", "main.py")
TEST_DIRS = ("tests",)
FRONT_DIR = "frontend"

GENERIC_EXC = {"KeyError", "TypeError", "ValueError", "AttributeError", "IndexError", "LookupError"}
EMPTY_LITERAL_SRC = {"{}", "[]", "''", '""', "0", "()", "False", "0.0", "dict()", "list()", "set()", "tuple()"}
SMUGGLE_KEY_RE = re.compile(r"(reader|repo|service|conn|connection|session|writer)", re.I)
DEFENSE_COMMENT_RE = re.compile(r"(兜底|防御性|以防万一|容错|保险起见|best[- ]effort|defensive|just in case|fallback)", re.I)
FRAMEWORK_DECORATOR_RE = re.compile(
    r"(route|get|post|put|delete|patch|before_request|after_request|teardown|errorhandler|"
    r"fixture|property|setter|getter|cached_property|abstractmethod|staticmethod|classmethod|"
    r"context_processor|app_template|template_filter|register|hookimpl|overload|contextmanager|"
    r"lru_cache|cache|wraps|dataclass|singledispatch)"
)


def rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def iter_py(root, dirs, extra_files=()):
    for d in dirs:
        base = os.path.join(root, d)
        for dp, dns, fns in os.walk(base):
            dns[:] = [x for x in dns if x not in ("__pycache__", "node_modules", "vendor", ".venv")]
            for fn in fns:
                if fn.endswith(".py"):
                    yield os.path.join(dp, fn)
    for f in extra_files:
        p = os.path.join(root, f)
        if os.path.exists(p):
            yield p


def src(node, text):
    try:
        s = ast.get_source_segment(text, node) or ""
    except Exception:
        s = ""
    return " ".join(s.split())[:200]


class Parents(ast.NodeVisitor):
    def __init__(self, tree):
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent


def enclosing_func(node):
    p = getattr(node, "_parent", None)
    while p is not None:
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return p
        p = getattr(p, "_parent", None)
    return None


def enclosing_class(node):
    p = getattr(node, "_parent", None)
    while p is not None:
        if isinstance(p, ast.ClassDef):
            return p
        p = getattr(p, "_parent", None)
    return None


def func_qual(node):
    f = enclosing_func(node)
    if f is None:
        return "<module>"
    c = enclosing_class(f)
    return (c.name + "." if c else "") + f.name


def is_empty_literal(node, text):
    return src(node, text) in EMPTY_LITERAL_SRC


def names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def exc_names(t):
    if t is None:
        return ["<bare>"]
    if isinstance(t, ast.Tuple):
        out = []
        for e in t.elts:
            out.extend(exc_names(e))
        return out
    if isinstance(t, ast.Name):
        return [t.id]
    if isinstance(t, ast.Attribute):
        return [t.attr]
    return ["?"]


def handler_reraises(h):
    for n in ast.walk(h):
        if isinstance(n, ast.Raise):
            return True
    return False


def decorator_names(fn):
    out = []
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        if isinstance(target, ast.Attribute):
            out.append(target.attr)
        elif isinstance(target, ast.Name):
            out.append(target.id)
    return out


def params_of(fn):
    a = fn.args
    pos = list(getattr(a, "posonlyargs", [])) + list(a.args)
    return pos, a.kwonlyargs, a.vararg, a.kwarg


def annotation_src(arg, text):
    return src(arg.annotation, text) if arg.annotation is not None else ""


class FileScan:
    def __init__(self, root, path, text, tree, prod):
        self.root = root
        self.path = rel(root, path)
        self.text = text
        self.tree = tree
        self.prod = prod
        self.lines = text.splitlines()
        Parents(tree)
        self.import_names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.import_names.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    self.import_names.add(a.asname or a.name)

    def hit(self, out, kind, node, **extra):
        rec = {"file": self.path, "line": getattr(node, "lineno", 0), "func": func_qual(node) if node is not None else ""}
        rec.update(extra)
        out[kind].append(rec)


def scan_python_file(fs, out):
    t = fs.text
    tree = fs.tree
    for node in ast.walk(tree):
        # 1 反射探测 #10 / F
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr", "setattr", "delattr"):
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                target = node.args[0]
                tsrc = src(target, t)
                on_module = isinstance(target, ast.Name) and target.id in fs.import_names
                fs.hit(out, "reflect_literal", node, call=node.func.id, attr=node.args[1].value, target=tsrc,
                       with_default=(node.func.id == "getattr" and len(node.args) >= 3), on_import_name=on_module,
                       code=src(node, t))
        # 2 宽泛 except #16 #37 #79 K
        if isinstance(node, ast.ExceptHandler):
            names = exc_names(node.type)
            generic = [n for n in names if n in GENERIC_EXC]
            broad = any(n in ("<bare>", "Exception", "BaseException") for n in names) or len(generic) >= 2
            if broad:
                fs.hit(out, "broad_except", node, types=names, reraises=handler_reraises(node),
                       body=src(node.body[0], t) if node.body else "")
            elif "ValueError" in names:
                # 103: json.loads(object_pairs_hook=...) 包在 except ValueError 里
                tr = getattr(node, "_parent", None)
                if isinstance(tr, ast.Try):
                    for b in tr.body:
                        for c in ast.walk(b):
                            if isinstance(c, ast.Call) and any(k.arg == "object_pairs_hook" for k in c.keywords):
                                fs.hit(out, "loads_hook_valueerror", node, code=src(c, t))
        # 3 or 空字面量 #78 O / A
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) >= 2:
            last = node.values[-1]
            if is_empty_literal(last, t):
                fs.hit(out, "falsy_or_default", node, code=src(node, t))
        # 4 往返/拷贝 #3 #11 #56 M
        if isinstance(node, ast.Call):
            fsrc = src(node.func, t)
            if fsrc in ("copy.deepcopy", "deepcopy", "copy.copy"):
                fs.hit(out, "copy_call", node, code=src(node, t))
            if fsrc in ("json.loads",) and node.args and isinstance(node.args[0], ast.Call) and src(node.args[0].func, t) == "json.dumps":
                fs.hit(out, "json_roundtrip", node, code=src(node, t))
            if fsrc == "int" and node.args and isinstance(node.args[0], ast.Call) and src(node.args[0].func, t) == "float":
                fs.hit(out, "int_via_float", node, code=src(node, t))
            if fsrc.endswith("fromisoformat") and node.args and isinstance(node.args[0], ast.Call) and src(node.args[0].func, t).endswith("isoformat"):
                fs.hit(out, "iso_roundtrip", node, code=src(node, t))
            if fsrc == "set" and node.args and isinstance(node.args[0], ast.Call) and src(node.args[0].func, t) == "list":
                inner = node.args[0]
                if inner.args and isinstance(inner.args[0], ast.Call) and src(inner.args[0].func, t) == "set":
                    fs.hit(out, "set_list_set", node, code=src(node, t))
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            elt = node.elt
            if isinstance(elt, ast.Call) and src(elt.func, t) == "dict" and len(elt.args) == 1 and isinstance(elt.args[0], ast.Name):
                gen = node.generators[0]
                if isinstance(gen.target, ast.Name) and gen.target.id == elt.args[0].id:
                    fs.hit(out, "dict_row_copy", node, code=src(node, t))
        # 5 raise 缓存异常实例 #105
        if isinstance(node, ast.Raise) and node.exc is not None and isinstance(node.exc, (ast.Attribute, ast.Subscript)):
            fs.hit(out, "raise_stored_exception", node, code=src(node, t))
        # 6 `_ = name` 骗 linter #17
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_" \
                and isinstance(node.value, (ast.Name, ast.Attribute)):
            fs.hit(out, "underscore_discard", node, code=src(node, t))
        # 7 往别人身上塞属性 / 函数属性夹带 #29 #96 I
        if isinstance(node, ast.Assign):
            for tg in node.targets:
                if isinstance(tg, ast.Attribute) and isinstance(tg.value, ast.Name) and tg.value.id not in ("self", "cls"):
                    fn = enclosing_func(node)
                    if fn is not None and tg.value.id not in fs.import_names:
                        fs.hit(out, "foreign_attr_assign", node, target=src(tg, t), code=src(node, t))
                if isinstance(tg, ast.Subscript) and isinstance(tg.slice, ast.Constant) and isinstance(tg.slice.value, str) \
                        and SMUGGLE_KEY_RE.search(tg.slice.value) and isinstance(node.value, (ast.Name, ast.Attribute)):
                    fs.hit(out, "dict_dependency_smuggle", node, code=src(node, t))
        # 8 模块导入期自检 #22
        if isinstance(node, ast.Assert) and enclosing_func(node) is None:
            fs.hit(out, "import_time_assert", node, code=src(node, t))

    # 函数级检查
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        pos, kwonly, vararg, kwarg = params_of(fn)
        allp = [a for a in pos + kwonly if a.arg not in ("self", "cls")]
        body = fn.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        decos = decorator_names(fn)
        cls = enclosing_class(fn)
        qual = (cls.name + "." if cls else "") + fn.name
        # 9 参数进门即被覆盖 #42 #33
        for a in allp:
            for stmt in body:
                used = names_in(stmt)
                if a.arg not in used:
                    continue
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    tgts = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    tnames = set()
                    for tg in tgts:
                        if isinstance(tg, ast.Name):
                            tnames.add(tg.id)
                        elif isinstance(tg, ast.Tuple):
                            tnames |= {e.id for e in tg.elts if isinstance(e, ast.Name)}
                    if a.arg in tnames and stmt.value is not None and a.arg not in names_in(stmt.value):
                        out["param_overwritten"].append({"file": fs.path, "line": stmt.lineno, "func": qual, "param": a.arg,
                                                         "code": src(stmt, fs.text)})
                break
        # 10 未使用参数 #17 A
        if body and not any(d and FRAMEWORK_DECORATOR_RE.search(d) for d in decos):
            only_stub = all(isinstance(s, (ast.Pass, ast.Raise)) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in body)
            if not only_stub:
                used = set()
                for s in body:
                    used |= names_in(s)
                for a in allp:
                    if a.arg.startswith("_") or a.arg in used:
                        continue
                    out["unused_param"].append({"file": fs.path, "line": fn.lineno, "func": qual, "param": a.arg,
                                                "is_method": cls is not None, "private_func": fn.name.startswith("_") and not fn.name.startswith("__")})
        # 11 透传包装 #4 #12 B
        if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Call) and not decos:
            call = body[0].value
            pnames = [a.arg for a in allp]
            argnames = []
            ok = True
            for x in call.args:
                if isinstance(x, ast.Name):
                    argnames.append(x.id)
                elif isinstance(x, ast.Starred) and isinstance(x.value, ast.Name):
                    argnames.append("*" + x.value.id)
                else:
                    ok = False
            for k in call.keywords:
                if isinstance(k.value, ast.Name) and (k.arg is None or k.arg == k.value.id):
                    argnames.append(k.value.id if k.arg else "**" + k.value.id)
                else:
                    ok = False
            star = (["*" + vararg.arg] if vararg else []) + (["**" + kwarg.arg] if kwarg else [])
            if ok and sorted(argnames) == sorted(pnames + star) and fn.name not in ("__call__",):
                out["passthrough_wrapper"].append({"file": fs.path, "line": fn.lineno, "func": qual, "callee": src(call.func, fs.text),
                                                   "is_method": cls is not None})
        # 12 注解为具体类型的参数又做 isinstance / is None 检查 F
        ann = {a.arg: annotation_src(a, fs.text) for a in allp}
        defaults_none = set()
        posdef = fn.args.defaults
        for a, d in zip(pos[len(pos) - len(posdef):], posdef):
            if isinstance(d, ast.Constant) and d.value is None:
                defaults_none.add(a.arg)
        for a, d in zip(kwonly, fn.args.kw_defaults):
            if isinstance(d, ast.Constant) and d.value is None:
                defaults_none.add(a.arg)
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "isinstance" and len(n.args) == 2 \
                    and isinstance(n.args[0], ast.Name) and n.args[0].id in ann:
                a_ann = ann[n.args[0].id]
                tsrc = src(n.args[1], fs.text)
                if a_ann and a_ann not in ("Any", "object") and "Union" not in a_ann and "Optional" not in a_ann and "|" not in a_ann \
                        and tsrc and (tsrc == a_ann or a_ann.endswith(tsrc) or tsrc.split(".")[-1] == a_ann.split("[")[0].split(".")[-1]):
                    out["isinstance_on_annotated"].append({"file": fs.path, "line": n.lineno, "func": qual, "param": n.args[0].id,
                                                           "annotation": a_ann, "code": src(n, fs.text)})
            if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name) and n.left.id in ann and len(n.ops) == 1 \
                    and isinstance(n.ops[0], (ast.Is, ast.IsNot)) and isinstance(n.comparators[0], ast.Constant) and n.comparators[0].value is None:
                a_ann = ann[n.left.id]
                if a_ann and n.left.id not in defaults_none and "Optional" not in a_ann and "None" not in a_ann and a_ann not in ("Any", "object"):
                    out["none_check_on_non_optional"].append({"file": fs.path, "line": n.lineno, "func": qual, "param": n.left.id,
                                                              "annotation": a_ann, "code": src(n, fs.text)})
        # 13 默认值兜底后假必填校验 #18
        for i, stmt in enumerate(body[:-1]):
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) \
                    and isinstance(stmt.value, ast.BoolOp) and isinstance(stmt.value.op, ast.Or):
                name = stmt.targets[0].id
                for nxt in body[i + 1:i + 4]:
                    if isinstance(nxt, ast.If):
                        tsrc = src(nxt.test, fs.text)
                        if tsrc in (f"{name} is None", f"not {name}") and any(isinstance(x, ast.Raise) for x in nxt.body):
                            out["default_then_required_check"].append({"file": fs.path, "line": stmt.lineno, "func": qual,
                                                                        "code": src(stmt, fs.text) + " ... " + tsrc})
        # 14 事务内套事务 / 事务内文件 I/O #62 #66 N
        fsrc = src(fn, fs.text) if False else None
        has_in_tx_assert = any(isinstance(n, ast.Attribute) and n.attr == "in_transaction" for n in ast.walk(fn))
        for n in ast.walk(fn):
            if isinstance(n, (ast.With, ast.AsyncWith)):
                ctx = " ".join(src(it.context_expr, fs.text) for it in n.items)
                if re.search(r"transaction|immediate|write_lock|savepoint", ctx, re.I):
                    if has_in_tx_assert:
                        out["assert_tx_then_new_tx"].append({"file": fs.path, "line": n.lineno, "func": qual, "ctx": ctx})
                    for c in ast.walk(n):
                        if isinstance(c, ast.Call):
                            cs = src(c.func, fs.text)
                            if re.search(r"(^open$|\.open$|write_text|write_bytes|read_text|read_bytes|json\.dump$|shutil\.|os\.replace|os\.remove|unlink|hashlib\.|sha256)", cs):
                                out["io_in_transaction"].append({"file": fs.path, "line": c.lineno, "func": qual, "ctx": ctx, "call": cs})


def collect_defs_and_refs(files):
    defs = collections.defaultdict(list)
    for fs in files:
        if not fs.prod:
            continue
        for fn in ast.walk(fs.tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if fn.name.startswith("__") and fn.name.endswith("__"):
                    continue
                cls = enclosing_class(fn)
                parent_fn = enclosing_func(fn)
                defs[fn.name].append({"file": fs.path, "line": fn.lineno, "kind": type(fn).__name__,
                                      "is_method": cls is not None and parent_fn is None, "nested": parent_fn is not None,
                                      "decos": decorator_names(fn) if not isinstance(fn, ast.ClassDef) else [], "node": fn, "fs": fs})
    refs_prod = collections.Counter()
    refs_test = collections.Counter()
    calls = collections.defaultdict(list)
    uncalled_refs = collections.Counter()
    for fs in files:
        counter = refs_prod if fs.prod else refs_test
        for n in ast.walk(fs.tree):
            name = None
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                name = n.id
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                name = n.attr
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                name = n.value
            elif isinstance(n, ast.alias):
                name = (n.name.split(".")[-1])
            if name is not None and name in defs:
                counter[name] += 1
                parent = getattr(n, "_parent", None)
                if isinstance(n, (ast.Name, ast.Attribute)) and not (isinstance(parent, ast.Call) and parent.func is n):
                    if not isinstance(parent, ast.Attribute):
                        uncalled_refs[(name, fs.prod)] += 1
            if isinstance(n, ast.Call):
                cname = n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func, ast.Attribute) else None)
                if cname in defs:
                    calls[cname].append({"fs": fs, "node": n})
    return defs, refs_prod, refs_test, calls, uncalled_refs


def cross_language_text(root):
    chunks = []
    for base in ("frontend", "tests", "templates", "static", "scripts", "packaging"):
        for dp, dns, fns in os.walk(os.path.join(root, base)):
            dns[:] = [x for x in dns if x not in ("node_modules", "__pycache__", "vendor")]
            for fn in fns:
                if fn.endswith((".js", ".jsx", ".cjs", ".mjs", ".html", ".json", ".bat", ".ps1", ".spec", ".txt")):
                    try:
                        with open(os.path.join(dp, fn), encoding="utf-8", errors="ignore") as f:
                            chunks.append(f.read())
                    except OSError:
                        pass
    for fn in os.listdir(root):
        if fn.endswith((".bat", ".spec", ".toml", ".json")):
            try:
                with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as f:
                    chunks.append(f.read())
            except OSError:
                pass
    return "\n".join(chunks)


def scan_references(files, root, out):
    defs, refs_prod, refs_test, calls, uncalled = collect_defs_and_refs(files)
    xlang = cross_language_text(root)
    for name, dl in defs.items():
        if len(dl) != 1 or len(name) < 4:
            continue
        d = dl[0]
        if d["nested"]:
            continue
        if any(FRAMEWORK_DECORATOR_RE.search(x) for x in d["decos"]):
            continue
        self_refs = 0
        rp = refs_prod[name] - self_refs
        rt = refs_test[name]
        if rp == 0:
            if re.search(r"\b" + re.escape(name) + r"\b", xlang):
                continue
            kind = "test_only_reference" if rt else "zero_reference"
            out[kind].append({"file": d["file"], "line": d["line"], "name": name, "def_kind": d["kind"],
                              "is_method": d["is_method"], "test_refs": rt})
    # 伪可选 / 恒常量开关 / 从不传参（只对全仓唯一函数名）
    for name, dl in defs.items():
        if len(dl) != 1 or dl[0]["kind"] == "ClassDef" or dl[0]["nested"]:
            continue
        d = dl[0]
        fn = d["node"]
        if uncalled[(name, True)] or uncalled[(name, False)]:
            continue  # 当回调传递，调用形态不可见
        pos, kwonly, vararg, kwarg = params_of(fn)
        offset = 1 if d["is_method"] and pos and pos[0].arg in ("self", "cls") else 0
        posdefs = fn.args.defaults
        defaulted = []
        for idx, (a, dv) in enumerate(zip(pos[len(pos) - len(posdefs):], posdefs)):
            defaulted.append((a.arg, pos.index(a) - offset, dv))
        for a, dv in zip(kwonly, fn.args.kw_defaults):
            if dv is not None:
                defaulted.append((a.arg, None, dv))
        if not defaulted:
            continue
        cl = calls.get(name, [])
        prod_calls = [c for c in cl if c["fs"].prod]
        test_calls = [c for c in cl if not c["fs"].prod]
        if not prod_calls:
            continue
        if any(any(isinstance(x, ast.Starred) for x in c["node"].args) or any(k.arg is None for k in c["node"].keywords) for c in cl):
            continue
        for pname, pidx, dv in defaulted:
            def passed(c):
                node = c["node"]
                if any(k.arg == pname for k in node.keywords):
                    return [k.value for k in node.keywords if k.arg == pname][0]
                if pidx is not None and len(node.args) > pidx:
                    return node.args[pidx]
                return None
            pv = [passed(c) for c in prod_calls]
            tv = [passed(c) for c in test_calls]
            dsrc = src(dv, d["fs"].text)
            if all(v is None for v in pv):
                out["param_never_passed_in_prod"].append({"file": d["file"], "line": fn.lineno, "func": name, "param": pname,
                                                          "default": dsrc, "prod_calls": len(prod_calls),
                                                          "test_passes": sum(1 for v in tv if v is not None)})
            elif all(v is not None for v in pv):
                vals = {src(v, c["fs"].text) for v, c in zip(pv, prod_calls)}
                consts = all(isinstance(v, ast.Constant) for v in pv)
                if dsrc == "None" and not any(isinstance(v, ast.Constant) and v.value is None for v in pv):
                    out["optional_always_passed"].append({"file": d["file"], "line": fn.lineno, "func": name, "param": pname,
                                                          "prod_calls": len(prod_calls), "values": sorted(vals)[:4]})
                if consts and len(vals) == 1:
                    out["param_constant_everywhere"].append({"file": d["file"], "line": fn.lineno, "func": name, "param": pname,
                                                             "default": dsrc, "value": next(iter(vals)), "prod_calls": len(prod_calls)})


def scan_modules(files, out):
    for fs in files:
        if not fs.prod:
            continue
        body = fs.tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]
        if not body:
            continue
        kinds = set()
        alias_only = True
        for s in body:
            if isinstance(s, (ast.Import, ast.ImportFrom)):
                kinds.add("import")
            elif isinstance(s, ast.Assign) and all(isinstance(tg, ast.Name) for tg in s.targets) and (
                    (len(s.targets) == 1 and s.targets[0].id == "__all__") or isinstance(s.value, (ast.Name, ast.Attribute))):
                kinds.add("alias")
            else:
                alias_only = False
                break
        if alias_only and "import" in kinds:
            n_import = sum(len(s.names) for s in body if isinstance(s, (ast.Import, ast.ImportFrom)))
            out["pure_reexport_module"].append({"file": fs.path, "line": 1, "is_init": fs.path.endswith("__init__.py"),
                                                "imported_names": n_import})
        for s in body:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name) and isinstance(s.value, ast.Name) \
                    and s.targets[0].id != "__all__" and s.targets[0].id != s.value.id and not s.targets[0].id.isupper():
                out["module_alias"].append({"file": fs.path, "line": s.lineno, "code": src(s, fs.text)})


def scan_comments(root, files, out):
    for fs in files:
        if not fs.prod:
            continue
        for i, line in enumerate(fs.lines, 1):
            m = DEFENSE_COMMENT_RE.search(line)
            if m and ("#" in line or '"""' in line or "'''" in line):
                out["defense_wording"].append({"file": fs.path, "line": i, "word": m.group(1), "code": line.strip()[:160]})
            if "type: ignore" in line:
                out["type_ignore"].append({"file": fs.path, "line": i, "code": line.strip()[:160]})
            if "pragma: no cover" in line:
                out["pragma_no_cover"].append({"file": fs.path, "line": i, "code": line.strip()[:160]})
            if re.search(r"#\s*noqa", line):
                out["noqa"].append({"file": fs.path, "line": i, "code": line.strip()[:160]})


def scan_tests(root, files, out):
    test_files = [fs for fs in files if not fs.prod]
    support = [fs for fs in test_files if os.path.basename(fs.path).endswith("_support.py") or "/_support/" in fs.path]
    importers = collections.defaultdict(set)
    for fs in test_files:
        for n in ast.walk(fs.tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                importers[n.module].add(fs.path)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    importers[a.name].add(fs.path)
    for fs in support:
        mod = fs.path[:-3].replace("/", ".")
        users = importers.get(mod, set())
        if len(users) <= 1:
            out["test_support_single_user"].append({"file": fs.path, "line": 1, "users": sorted(users)})
    for fs in test_files:
        for n in ast.walk(fs.tree):
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and n.attr.startswith("_legacy"):
                out["test_calls_legacy_private"].append({"file": fs.path, "line": n.lineno, "code": src(n, fs.text)})
            if isinstance(n, ast.Assign):
                for tg in n.targets:
                    if isinstance(tg, ast.Attribute) and isinstance(tg.value, ast.Attribute | ast.Name) and enclosing_func(n) is not None:
                        base = src(tg.value, fs.text)
                        if base in fs.import_names or "." in base and base.split(".")[0] in fs.import_names:
                            out["test_global_module_patch"].append({"file": fs.path, "line": n.lineno, "code": src(n, fs.text)})
    # .cjs 字面量文档测试 #9
    for dp, dns, fns in os.walk(os.path.join(root, "tests")):
        for fn in fns:
            if fn.endswith(".cjs"):
                p = os.path.join(dp, fn)
                with open(p, encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
                n = len(re.findall(r"readFileSync\([^)]*\)[^;\n]{0,120}\.includes\(", txt)) + len(re.findall(r"\.includes\(['\"`]", txt))
                if n:
                    out["cjs_literal_includes"].append({"file": rel(root, p), "line": 1, "count": n})


JS_PATTERNS = {
    "js_or_empty": r"\|\|\s*(''|\"\"|\[\]|\{\}|0\b|null\b)",
    "js_nullish_empty": r"\?\?\s*(''|\"\"|\[\]|\{\}|0\b|null\b)",
    "js_optional_chain": r"\?\.",
    "js_slice_copy": r"\.slice\(\s*\)",
    "js_json_clone": r"JSON\.parse\(\s*JSON\.stringify\(",
    "js_structured_clone": r"structuredClone\(",
    "js_typeof_function": r"typeof\s+[\w.]+\s*===?\s*['\"]function['\"]",
    "js_empty_catch": r"catch\s*(\([^)]*\))?\s*\{\s*\}",
    "js_switch_default": r"\bdefault\s*:",
    "js_array_isarray": r"Array\.isArray\(",
}


def scan_frontend(root, out):
    base = os.path.join(root, FRONT_DIR)
    per_file = {}
    for dp, dns, fns in os.walk(base):
        dns[:] = [x for x in dns if x not in ("node_modules", "vendor")]
        for fn in fns:
            if not fn.endswith((".js", ".jsx")):
                continue
            p = os.path.join(dp, fn)
            with open(p, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            counts = {}
            for k, pat in JS_PATTERNS.items():
                c = len(re.findall(pat, txt))
                if c:
                    counts[k] = c
            # #112 默认 flag 参数被直接绑定为事件处理器
            for m in re.finditer(r"(?:function\s+(\w+)\s*\(\s*\w+\s*=\s*(?:false|true)|const\s+(\w+)\s*=\s*\(\s*\w+\s*=\s*(?:false|true)\s*\)\s*=>)", txt):
                name = m.group(1) or m.group(2)
                if re.search(r"on[A-Z]\w*=\{\s*" + re.escape(name) + r"\s*\}", txt):
                    out["js_default_flag_bound_as_handler"].append({"file": rel(root, p), "line": txt[:m.start()].count("\n") + 1, "name": name})
            per_file[rel(root, p)] = {"lines": txt.count("\n") + 1, **counts}
    out["js_file_counts"] = [dict(file=k, **v) for k, v in sorted(per_file.items())]


def main():
    ap = argparse.ArgumentParser(description="全量反过度防御 AST / JS 深度机械扫描器")
    ap.add_argument("--root", default=".", help="待扫描仓库根目录")
    ap.add_argument("--out", required=True, help="扫描结果输出目录（生成 hits.json）")
    ap.add_argument("--prod-dirs", default="", help="逗号分隔的生产代码目录（默认自动探测）")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    if args.prod_dirs:
        prod_dirs = tuple(x.strip() for x in args.prod_dirs.split(",") if x.strip())
    else:
        prod_dirs = tuple(d for d in PROD_DIRS if os.path.isdir(os.path.join(root, d)))
        if not prod_dirs:
            ignored = {"__pycache__", "node_modules", "vendor", ".venv", "venv", "tests", "docs", "dist", "build"}
            prod_dirs = tuple(
                d for d in sorted(os.listdir(root))
                if not d.startswith(".") and d not in ignored and os.path.isdir(os.path.join(root, d))
            )
    out = collections.defaultdict(list)
    files = []
    errors = []
    for path, prod in [(p, True) for p in iter_py(root, prod_dirs, PROD_ROOT_FILES)] + [(p, False) for p in iter_py(root, TEST_DIRS)]:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as exc:
            errors.append(f"{path}: {exc}")
            continue
        files.append(FileScan(root, path, text, tree, prod))
    for fs in files:
        if fs.prod:
            scan_python_file(fs, out)
    scan_references(files, root, out)
    scan_modules(files, out)
    scan_comments(root, files, out)
    scan_tests(root, files, out)
    scan_frontend(root, out)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "hits.json"), "w", encoding="utf-8") as f:
        json.dump({"errors": errors, "hits": out}, f, ensure_ascii=False, indent=1)
    summary = {k: len(v) for k, v in sorted(out.items())}
    print(json.dumps({"files": len(files), "parse_errors": len(errors), "summary": summary}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
