"""被扫描代码的静态结构：模块、函数、类、MRO 与名字的静态解析。

函数身份以调用图记录为准：定义节点按 (文件, 行, 名) 对上记录就用记录的限定名；测试文件与调用图不索引的
定义（嵌套类的方法、模块顶层复合语句里的定义）按同一命名规则给出限定名，只作引用来源，不作扫描目标。
"""
from __future__ import annotations

import ast
import builtins
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple, Union

from .ast_nodes import FUNCTION_NODES, lambda_label
from .binder import Signature, signature_of
from .imports import CLASS, DEF, EXTERNAL, MODULE, ModuleIndex, ModuleInfo, Symbol
from .model import FunctionRecord, rel_join
from .protocols import TRANSPARENT_DECORATORS
from .scope import Scope
from .values import EMPTY, ClassObj, ExternalRef, Func, ModuleRef, Values

BUILTIN_NAMES = frozenset(dir(builtins))
FUNCTION = "function"
METHOD = "method"
STATIC = "staticmethod"
CLASSMETHOD = "classmethod"
GETTER = "property"
SETTER = "setter"
LAMBDA = "lambda"
_KIND_BY_DECORATOR = {
    "builtins.staticmethod": STATIC,
    "builtins.classmethod": CLASSMETHOD,
    "builtins.property": GETTER,
    "functools.cached_property": GETTER,
}


FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda]


class FunctionInfo:
    def __init__(self, qual: str, name: str, module: ModuleInfo, node: FunctionNode, owner: Optional[ClassInfo]) -> None:
        self.qual = qual
        self.name = name
        self.module = module
        self.rel = module.rel
        self.node = node
        self.args = node.args
        self.scope: Scope = module.scopes[id(node)]
        self.owner = owner
        self.kind = LAMBDA if isinstance(node, ast.Lambda) else FUNCTION
        self.signature: Optional[Signature] = None
        self.is_generator = False


class ClassInfo:
    def __init__(self, cid: str, module: ModuleInfo, node: ast.ClassDef) -> None:
        self.cid = cid
        self.module = module
        self.rel = module.rel
        self.node = node
        self.name = node.name
        self.methods: Dict[str, FunctionInfo] = {}
        self.setters: Dict[str, FunctionInfo] = {}
        self.bases: List[str] = []
        self.external_bases: List[str] = []
        self.fields: List[Tuple[str, Optional[ast.expr]]] = []
        self.generated_init = False
        self.is_namedtuple = False
        self.mro: List[str] = []


class Program:
    def __init__(self, repo_root: str, records: Dict[str, FunctionRecord], rels: Sequence[str]) -> None:
        modules = []
        for rel in rels:
            with open(rel_join(repo_root, rel), encoding="utf-8") as handle:
                modules.append(ModuleInfo(rel, ast.parse(handle.read(), filename=rel, type_comments=True)))
        self.index = ModuleIndex(modules, repo_root)
        self.modules = self.index.by_rel
        self._record_quals = {(record.rel, record.line, record.name): record.qual for record in records.values()}
        self.functions: Dict[int, FunctionInfo] = {}
        self.by_qual: Dict[str, FunctionInfo] = {}
        self.classes: Dict[str, ClassInfo] = {}
        self.class_by_node: Dict[int, ClassInfo] = {}
        for info in modules:
            for node in info.tree.body:
                if isinstance(node, ast.ClassDef):
                    self._add_class(info, node)
            self._index_nodes(info, info.tree.body, "", None)
        for cls in self.classes.values():
            self._resolve_class(cls)
        for function in self.functions.values():
            self._resolve_function(function)
            if function.kind != SETTER:
                self.by_qual[function.qual] = function
        for cid in self.classes:
            self._linearize(cid, ())

    # --- 索引 -------------------------------------------------------------------------------------------

    def _add_class(self, info: ModuleInfo, node: ast.ClassDef) -> None:
        cls = ClassInfo(f"{info.rel}::{node.name}", info, node)
        self.classes[cls.cid] = cls
        self.class_by_node[id(node)] = cls

    def _index_nodes(self, info: ModuleInfo, nodes: Iterable[ast.AST], prefix: str, owner: Optional[ClassInfo]) -> None:
        """按调用图的命名规则给每个 def / lambda 定名；owner 只给类体的直接子 def。

        装饰器、默认值、基类在外层作用域求值，其中的 lambda 归外层。
        """
        for child in nodes:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                label = lambda_label(child) if isinstance(child, ast.Lambda) else child.name
                qual = self._record_quals.get((info.rel, child.lineno, label), f"{info.rel}::{prefix}{label}")
                self.functions[id(child)] = FunctionInfo(qual, label, info, child, owner)
                outer: List[ast.AST] = [*child.args.defaults, *(value for value in child.args.kw_defaults if value is not None)]
                if not isinstance(child, ast.Lambda):
                    outer.extend(child.decorator_list)
                self._index_nodes(info, outer, prefix, None)
                body: List[ast.AST] = [child.body] if isinstance(child, ast.Lambda) else list(child.body)
                self._index_nodes(info, body, f"{prefix}{label}.<locals>.", None)
            elif isinstance(child, ast.ClassDef):
                self._index_nodes(info, [*child.decorator_list, *child.bases, *(keyword.value for keyword in child.keywords)], prefix, None)
                self._index_nodes(info, child.body, f"{prefix}{child.name}.", self.class_by_node.get(id(child)))
            else:
                self._index_nodes(info, ast.iter_child_nodes(child), prefix, None)

    def _resolve_class(self, cls: ClassInfo) -> None:
        for base in cls.node.bases:
            for symbol in self.static_expr(cls.module.scope, cls.module, base.value if isinstance(base, ast.Subscript) else base):
                if symbol.kind == CLASS and id(symbol.node) in self.class_by_node:
                    cls.bases.append(self.class_by_node[id(symbol.node)].cid)
                elif symbol.kind == EXTERNAL:
                    cls.external_bases.append(symbol.module)
        cls.is_namedtuple = "typing.NamedTuple" in cls.external_bases
        is_dataclass = False
        for decorator in cls.node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if any(symbol.module == "dataclasses.dataclass" for symbol in self.static_expr(cls.module.scope, cls.module, target)):
                is_dataclass = True
                cls.generated_init = not (isinstance(decorator, ast.Call) and any(
                    keyword.arg == "init" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                    for keyword in decorator.keywords
                ))
        if is_dataclass or cls.is_namedtuple:
            cls.fields = list(self._own_fields(cls))

    def _own_fields(self, cls: ClassInfo) -> Iterable[Tuple[str, Optional[ast.expr]]]:
        for node in cls.node.body:
            if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
                continue
            annotation = node.annotation.value if isinstance(node.annotation, ast.Subscript) else node.annotation
            if any(symbol.module == "typing.ClassVar" for symbol in self.static_expr(cls.module.scope, cls.module, annotation)):
                continue
            default = node.value
            if isinstance(default, ast.Call) and any(
                keyword.arg == "init" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                for keyword in default.keywords
            ):
                continue
            if isinstance(default, ast.Call) and not any(keyword.arg in ("default", "default_factory") for keyword in default.keywords) \
                    and any(symbol.module == "dataclasses.field" for symbol in self.static_expr(cls.module.scope, cls.module, default.func)):
                default = None
            yield node.target.id, default

    def _resolve_function(self, function: FunctionInfo) -> None:
        node = function.node
        if isinstance(node, ast.Lambda):
            function.signature = signature_of(node.args, exact=True)
            return
        assert isinstance(node, FUNCTION_NODES)
        outer = function.scope.parent
        assert outer is not None
        for decorator in node.decorator_list:
            for name in self.decorator_names(outer, function.module, decorator):
                if name in _KIND_BY_DECORATOR and function.owner is not None:
                    function.kind = _KIND_BY_DECORATOR[name]
            if self._is_setter(function, decorator):
                function.kind = SETTER
        function.is_generator = _is_generator(node)
        transparent = all(self.is_transparent(function, decorator) for decorator in node.decorator_list)
        function.signature = signature_of(node.args, exact=transparent)
        if function.owner is not None:
            if function.kind == FUNCTION:
                function.kind = METHOD
            table = function.owner.setters if function.kind == SETTER else function.owner.methods
            table[function.name] = function

    def is_transparent(self, function: FunctionInfo, decorator: ast.expr) -> bool:
        """装饰后仍按原名、原签名调用（property、staticmethod、lru_cache 等，及 property 的 setter）。"""
        if self._is_setter(function, decorator):
            return True
        outer = function.scope.parent
        assert outer is not None
        names = self.decorator_names(outer, function.module, decorator)
        return bool(names) and all(name in TRANSPARENT_DECORATORS for name in names)

    def _is_setter(self, function: FunctionInfo, decorator: ast.expr) -> bool:
        return (
            function.owner is not None
            and isinstance(decorator, ast.Attribute)
            and decorator.attr in ("setter", "deleter")
            and isinstance(decorator.value, ast.Name)
            and decorator.value.id in function.owner.methods
        )

    def _linearize(self, cid: str, stack: Tuple[str, ...]) -> List[str]:
        cls = self.classes[cid]
        if cls.mro:
            return cls.mro
        if cid in stack:
            raise ValueError(f"类继承成环：{' -> '.join(stack + (cid,))}")
        sequences = [list(self._linearize(base, stack + (cid,))) for base in cls.bases] + [list(cls.bases)]
        result = [cid]
        while any(sequences):
            for sequence in sequences:
                if sequence and not any(sequence[0] in other[1:] for other in sequences):
                    head = sequence[0]
                    break
            else:
                raise ValueError(f"{cid} 的基类顺序无法线性化")
            result.append(head)
            for sequence in sequences:
                if sequence and sequence[0] == head:
                    sequence.pop(0)
        cls.mro = result
        return result

    # --- 静态解析 ---------------------------------------------------------------------------------------

    def static_name(self, scope: Scope, module: ModuleInfo, name: str) -> Tuple[Symbol, ...]:
        """名字在 scope 里的静态符号：只认模块级绑定与内置名；函数局部名不做静态解析。"""
        owner = scope.binding_scope(name)
        if owner is None:
            return (Symbol(EXTERNAL, self.index.externals.builtin(name), ""),) if name in BUILTIN_NAMES else ()
        if owner.parent is None:
            return self.index.module_symbols(module.name, name)
        return ()

    def static_expr(self, scope: Scope, module: ModuleInfo, node: ast.AST) -> Tuple[Symbol, ...]:
        if isinstance(node, ast.Name):
            return self.static_name(scope, module, node.id)
        if isinstance(node, ast.Attribute):
            found: List[Symbol] = []
            for symbol in self.static_expr(scope, module, node.value):
                if symbol.kind == MODULE:
                    found.extend(self.index.module_symbols(symbol.module, node.attr))
                elif symbol.kind == EXTERNAL:
                    attribute = self.index.externals.attribute(symbol.module, node.attr)
                    if attribute is not None:
                        found.append(Symbol(EXTERNAL, attribute, ""))
            return tuple(found)
        return ()

    def decorator_names(self, scope: Scope, module: ModuleInfo, decorator: ast.expr) -> List[str]:
        """装饰器的外部全名；仓库内或解析不了的装饰器不给名字。"""
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        return [symbol.module for symbol in self.static_expr(scope, module, target) if symbol.kind == EXTERNAL]

    def symbol_values(self, symbols: Iterable[Symbol]) -> Values:
        """静态符号对应的值；模块级变量没有静态值，由调用方读传播结果。"""
        values = set()
        for symbol in symbols:
            if symbol.kind == DEF:
                values.add(Func(self.functions[id(symbol.node)].qual))
            elif symbol.kind == CLASS:
                cls = self.class_by_node.get(id(symbol.node))
                if cls is not None:
                    values.add(ClassObj(cls.cid))
            elif symbol.kind == MODULE:
                values.add(ModuleRef(symbol.module))
            elif symbol.kind == EXTERNAL:
                values.add(ExternalRef(symbol.module))
        return frozenset(values) if values else EMPTY

    def lookup(self, cid: str, name: str, start_after: Optional[str] = None) -> Optional[FunctionInfo]:
        """沿 MRO 找方法；start_after 给出时从该类之后开始（``super()``）。

        ``__init__`` 先遇到 dataclass / NamedTuple 生成的初始化时，真正执行的是生成的那个，没有可记的方法。
        """
        if name == "__init__":
            return self.init_provider(cid, start_after)[0]
        for owner in self._mro_after(cid, start_after):
            method = self.classes[owner].methods.get(name)
            if method is not None:
                return method
        return None

    def init_provider(self, cid: str, start_after: Optional[str] = None) -> Tuple[Optional[FunctionInfo], Optional[ClassInfo]]:
        """构造时真正执行的 __init__：显式定义的方法，或 dataclass / NamedTuple 生成的初始化所在的类。"""
        for owner in self._mro_after(cid, start_after):
            cls = self.classes[owner]
            if "__init__" in cls.methods:
                return cls.methods["__init__"], None
            if cls.generated_init or cls.is_namedtuple:
                return None, cls
        return None, None

    def _mro_after(self, cid: str, start_after: Optional[str]) -> List[str]:
        mro = self.classes[cid].mro
        if start_after is None:
            return mro
        return mro[mro.index(start_after) + 1:] if start_after in mro else []

    def external_ancestry(self, cid: str) -> FrozenSet[str]:
        """仓库类真实继承链上的全部外部类：沿仓库 MRO 收集外部基类，再展开它们在已安装库里的继承链。"""
        return frozenset(
            name for owner in self.classes[cid].mro for base in self.classes[owner].external_bases
            for name in self.index.externals.mro(base)
        )

    def init_fields(self, cid: str) -> List[Tuple[str, Optional[ast.expr], str]]:
        """生成的初始化按 dataclass 规则收集的字段：(名, 默认值, 声明类)，基类字段在前。"""
        fields: Dict[str, Tuple[Optional[ast.expr], str]] = {}
        for owner in reversed(self.classes[cid].mro):
            cls = self.classes[owner]
            for name, default in cls.fields:
                fields[name] = (default, owner)
        return [(name, default, owner) for name, (default, owner) in fields.items()]


def _is_generator(node: ast.AST) -> bool:
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(current, (*FUNCTION_NODES, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(current))
    return False
