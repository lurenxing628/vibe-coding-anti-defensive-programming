"""Python 词法作用域表：每个模块、类、函数、lambda、推导式各一张，名字查找按 LEGB 规则。

扫描只记录“哪些名字绑定在哪个作用域”，不求值；求值由类型传播在遍历时按这里给出的绑定作用域取值。
"""
from __future__ import annotations

import ast
from typing import Dict, Iterable, Optional, Set

from .ast_nodes import COMPREHENSION_NODES, FUNCTION_NODES

MODULE = "module"
CLASS = "class"
FUNCTION = "function"
LAMBDA = "lambda"
COMPREHENSION = "comprehension"


class Scope:
    __slots__ = ("kind", "node", "parent", "locals", "declared_global", "declared_nonlocal")

    def __init__(self, kind: str, node: ast.AST, parent: Optional[Scope]) -> None:
        self.kind = kind
        self.node = node
        self.parent = parent
        self.locals: Set[str] = set()
        self.declared_global: Set[str] = set()
        self.declared_nonlocal: Set[str] = set()

    def module_scope(self) -> Scope:
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def binding_scope(self, name: str) -> Optional[Scope]:
        """名字在这里被读取时绑定在哪个作用域；None 表示内置名或未绑定。

        类作用域只对自己的类体直接可见：方法与类体内的推导式都跳过它。
        """
        scope: Optional[Scope] = self
        own = True
        while scope is not None:
            if scope.kind == CLASS and not own:
                scope = scope.parent
                continue
            if name in scope.declared_global:
                return scope.module_scope()
            if name in scope.locals and name not in scope.declared_nonlocal:
                return scope
            scope = scope.parent
            own = False
        return None


def build_scopes(tree: ast.Module) -> Dict[int, Scope]:
    """返回 id(作用域节点) → Scope；作用域节点是模块、类、函数、lambda 与推导式。"""
    scopes: Dict[int, Scope] = {}
    _build(tree, MODULE, None, scopes)
    return scopes


def _build(node: ast.AST, kind: str, parent: Optional[Scope], scopes: Dict[int, Scope]) -> Scope:
    scope = Scope(kind, node, parent)
    scopes[id(node)] = scope
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        scope.locals.update(_param_names(node.args))
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        _scan_all(node.body, scope, scopes)
    elif isinstance(node, ast.Lambda):
        _scan(node.body, scope, scopes)
    elif isinstance(node, COMPREHENSION_NODES):
        for index, generator in enumerate(node.generators):
            _scan(generator.target, scope, scopes)
            if index:
                _scan(generator.iter, scope, scopes)
            _scan_all(generator.ifs, scope, scopes)
        if isinstance(node, ast.DictComp):
            _scan(node.key, scope, scopes)
            _scan(node.value, scope, scopes)
        else:
            _scan(node.elt, scope, scopes)
    return scope


def _param_names(args: ast.arguments) -> Set[str]:
    names = {arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _scan_all(nodes: Iterable[ast.AST], scope: Scope, scopes: Dict[int, Scope]) -> None:
    for node in nodes:
        _scan(node, scope, scopes)


def _scan(node: ast.AST, scope: Scope, scopes: Dict[int, Scope]) -> None:
    if isinstance(node, FUNCTION_NODES):
        scope.locals.add(node.name)
        _scan_all(node.decorator_list, scope, scopes)
        _scan_signature(node.args, scope, scopes)
        if node.returns is not None:
            _scan(node.returns, scope, scopes)
        _build(node, FUNCTION, scope, scopes)
    elif isinstance(node, ast.ClassDef):
        scope.locals.add(node.name)
        _scan_all([*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)], scope, scopes)
        _build(node, CLASS, scope, scopes)
    elif isinstance(node, ast.Lambda):
        _scan_signature(node.args, scope, scopes)
        _build(node, LAMBDA, scope, scopes)
    elif isinstance(node, COMPREHENSION_NODES):
        _scan(node.generators[0].iter, scope, scopes)
        _build(node, COMPREHENSION, scope, scopes)
    elif isinstance(node, ast.Name):
        if not isinstance(node.ctx, ast.Load):
            scope.locals.add(node.id)
    elif isinstance(node, ast.NamedExpr):
        owner = scope
        while owner.kind == COMPREHENSION and owner.parent is not None:
            owner = owner.parent
        owner.locals.add(node.target.id)
        _scan(node.value, scope, scopes)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            if alias.name != "*":
                scope.locals.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.Global):
        scope.declared_global.update(node.names)
    elif isinstance(node, ast.Nonlocal):
        scope.declared_nonlocal.update(node.names)
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            scope.locals.add(node.name)
        _scan_all(ast.iter_child_nodes(node), scope, scopes)
    else:
        _scan_all(ast.iter_child_nodes(node), scope, scopes)


def _scan_signature(args: ast.arguments, scope: Scope, scopes: Dict[int, Scope]) -> None:
    """默认值与注解在定义处求值，属于外层作用域。"""
    defaults = [*args.defaults, *(value for value in args.kw_defaults if value is not None)]
    annotations = [
        arg.annotation
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if arg is not None and arg.annotation is not None
    ]
    _scan_all([*defaults, *annotations], scope, scopes)
