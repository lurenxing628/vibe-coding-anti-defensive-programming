"""类型注解 → 抽象值：仓库类给实例，仓库外的类给 ``ExternalInst``，typing 的容器、元组、Optional/Union、Type、
Callable 给对应结构。

注解里的名字按静态符号解析（模块级绑定与内置名），所以前向引用与 ``from __future__ import annotations``
都按最终定义取值。Python 3.8 的下标表达式包在 ``ast.Index`` 里，由 ``subscript_index`` 统一拆开。
"""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING, List, Optional

from .ast_nodes import subscript_index
from .imports import CLASS, EXTERNAL
from .scope import Scope
from .values import EMPTY, CallableT, ClassObj, Inst, MapT, SeqT, Tup, Values, external_instance, join

if TYPE_CHECKING:
    from .imports import ModuleInfo
    from .program import Program

_TYPING_MODULES = ("typing.", "typing_extensions.", "collections.abc.", "collections.", "builtins.", "dataclasses.")
_SEQUENCES = frozenset({
    "List", "list", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection", "Set", "set", "FrozenSet",
    "frozenset", "AbstractSet", "MutableSet", "Deque", "deque", "Generator", "AsyncIterator", "AsyncIterable",
    "AsyncGenerator", "Reversible", "KeysView", "ValuesView",
})
_MAPPINGS = frozenset({"Dict", "dict", "Mapping", "MutableMapping", "DefaultDict", "defaultdict", "OrderedDict", "ChainMap"})
_FIRST_ARGUMENT = frozenset({"Optional", "ClassVar", "Final", "Annotated", "InitVar"})


def annotation_values(program: Program, scope: Scope, module: ModuleInfo, node: Optional[ast.AST]) -> Values:
    if node is None:
        return EMPTY
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return annotation_values(program, scope, module, ast.parse(node.value.strip(), mode="eval").body)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return join(*(_class_instances(program, symbol) for symbol in program.static_expr(scope, module, node)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return join(annotation_values(program, scope, module, node.left), annotation_values(program, scope, module, node.right))
    if isinstance(node, ast.Subscript):
        return _subscript_values(program, scope, module, node)
    return EMPTY


def type_comment_values(program: Program, scope: Scope, module: ModuleInfo, comment: str) -> Values:
    return annotation_values(program, scope, module, ast.parse(comment, mode="eval").body)


def function_type_comment(comment: str) -> ast.FunctionType:
    tree = ast.parse(comment, mode="func_type")
    assert isinstance(tree, ast.FunctionType)
    return tree


def _class_instances(program: Program, symbol) -> Values:
    if symbol.kind == CLASS:
        cls = program.class_by_node.get(id(symbol.node))
        return frozenset({Inst(cls.cid)}) if cls is not None else EMPTY
    if symbol.kind == EXTERNAL:
        return external_instance(program.index.externals, symbol.module)
    return EMPTY


def _generic_kind(program: Program, scope: Scope, module: ModuleInfo, head: ast.AST) -> Optional[str]:
    for symbol in program.static_expr(scope, module, head):
        if symbol.kind == EXTERNAL and symbol.module.startswith(_TYPING_MODULES):
            return symbol.module.rpartition(".")[2]
    return None


def _subscript_values(program: Program, scope: Scope, module: ModuleInfo, node: ast.Subscript) -> Values:
    inner = subscript_index(node)
    args: List[ast.AST] = list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
    kind = _generic_kind(program, scope, module, node.value)
    if kind is None:
        return annotation_values(program, scope, module, node.value)

    def values(arg: ast.AST) -> Values:
        return annotation_values(program, scope, module, arg)

    if kind in _FIRST_ARGUMENT:
        return values(args[0])
    if kind == "Union":
        return join(*(values(arg) for arg in args))
    if kind in ("Tuple", "tuple"):
        if len(args) == 2 and isinstance(args[1], ast.Constant) and args[1].value is Ellipsis:
            return frozenset({SeqT(values(args[0]))})
        return frozenset({Tup(tuple(values(arg) for arg in args))})
    if kind in _SEQUENCES:
        return frozenset({SeqT(values(args[0]))})
    if kind in _MAPPINGS and len(args) == 2:
        return frozenset({MapT(values(args[0]), values(args[1]))})
    if kind in ("Type", "type"):
        return frozenset(ClassObj(value.cls) for value in values(args[0]) if isinstance(value, Inst))
    if kind == "Callable" and len(args) == 2:
        return frozenset({CallableT(values(args[1]))})
    return EMPTY
