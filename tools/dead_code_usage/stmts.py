"""语句遍历：赋值与拆包、分支合并、循环、with、try、import、def 与 class。"""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Callable, Dict, List, Tuple

from . import calls
from .ast_nodes import subscript_index
from .exprs import binary_value, subscript_load
from .frames import Frame
from .model import DECORATOR
from .protocols import BINARY_OPERATORS, SUBSCRIPT_MEMBERS, TRUTH
from .solver import field_key
from .type_hints import annotation_values, type_comment_values
from .values import EMPTY, ClassObj, Func, Inst, Method, SeqT, Site, Tup, Values, join

if TYPE_CHECKING:
    from .walker import Walker

Handler = Callable[["Walker", ast.AST], None]
Env = Dict[str, Values]
# 新建容器的原地运算里把右边的元素加进容器的那些：列表 ``+=``（同 extend），集合 ``|=`` 与 ``^=``（同 update 与
# symmetric_difference_update）。``-=``、``&=`` 只去掉元素。
_IN_PLACE_ADDS: Dict[str, Tuple[type, ...]] = {"list": (ast.Add,), "set": (ast.BitOr, ast.BitXor)}


def _snapshot(w: Walker) -> Env:
    return dict(w.frame.names)


def _merge(*envs: Env) -> Env:
    merged: Env = {}
    for env in envs:
        for name, values in env.items():
            merged[name] = join(merged[name], values) if name in merged else values
    return merged


def _expr(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Expr)
    w.ev(node.value)


def _assign(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Assign)
    values = w.ev(node.value)
    if node.type_comment:
        values = join(values, type_comment_values(w.program, w.frame.scope, w.module, node.type_comment))
    for target in node.targets:
        w.bind(target, values)


def _ann_assign(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.AnnAssign)
    declared = annotation_values(w.program, w.frame.scope, w.module, node.annotation)
    values = join(declared, w.ev(node.value)) if node.value is not None else declared
    target = node.target
    frame = w.frame
    if isinstance(target, ast.Name) and frame.cls is not None:
        w.solver.write(field_key(frame.cls.cid, target.id), values)
        if node.value is not None:
            frame.names[target.id] = values
    elif node.value is not None:
        w.bind(target, values)
    elif isinstance(target, ast.Name) and frame.function is not None:
        frame.names[target.id] = declared


def _aug_assign(w: Walker, node: ast.AST) -> None:
    """``x op= v``：新建的内置容器原地改写并返回自身，保持容器身份（``_IN_PLACE_ADDS`` 里的运算把 v 的元素加进去，
    不试 v 的反射方法）；其余取值与 ``x = x op v`` 同一套派发（先 ``__iop__``，没有再 ``__op__``），新值是被调方法的返回值。"""
    assert isinstance(node, ast.AugAssign)
    target = node.target
    if isinstance(target, ast.Name):
        current = w.load_name(target.id, target, callee=True)
    elif isinstance(target, ast.Attribute):
        current = w.attribute_load(w.ev(target.value), target.attr, target, callee=True)
    else:
        assert isinstance(target, ast.Subscript)
        index = subscript_index(target)
        current = subscript_load(w, w.ev(target.value), index, w.ev(index), target)
    value = w.ev(node.value)
    forward, _reflected, inplace = BINARY_OPERATORS[type(node.op)]
    sites = frozenset(site for site in current if isinstance(site, Site))
    for site in sites:
        if isinstance(node.op, _IN_PLACE_ADDS.get(site.kind, ())):
            calls.add_elements(w, site, w.iterate(value, node), node)
    others = current - sites
    result: Values = sites
    if others or not current:
        result = join(sites, binary_value(w, others, value, node.op, (inplace, forward), node))
    w.bind(target, result)


def _for(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, (ast.For, ast.AsyncFor))
    elements = w.iterate(w.ev(node.iter), node.iter)
    before = _snapshot(w)
    w.bind(node.target, elements)
    w.body(node.body)
    w.frame.names = _merge(before, w.frame.names)
    w.body(node.orelse)


def _while(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.While)
    w.protocol(w.ev(node.test), TRUTH, node.test)
    before = _snapshot(w)
    w.body(node.body)
    w.frame.names = _merge(before, w.frame.names)
    w.body(node.orelse)


def _if(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.If)
    w.protocol(w.ev(node.test), TRUTH, node.test)
    before = _snapshot(w)
    w.body(node.body)
    after_body = w.frame.names
    w.frame.names = before
    w.body(node.orelse)
    w.frame.names = _merge(after_body, w.frame.names)


def _with(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, (ast.With, ast.AsyncWith))
    enter, leave = ("__aenter__", "__aexit__") if isinstance(node, ast.AsyncWith) else ("__enter__", "__exit__")
    for item in node.items:
        manager = w.ev(item.context_expr)
        entered = [w.protocol(manager, (enter,), item.context_expr)]
        w.protocol(manager, (leave,), item.context_expr)
        # contextmanager 装饰的生成器函数：调用结果是生成器，as 拿到的是它 yield 的值。
        entered.extend(value.elems for value in manager if isinstance(value, SeqT))
        if item.optional_vars is not None:
            w.bind(item.optional_vars, join(*entered))
    w.body(node.body)


def _exception_instances(values: Values) -> Values:
    found = set()
    for value in values:
        if isinstance(value, ClassObj):
            found.add(Inst(value.cls))
        elif isinstance(value, Tup):
            found.update(Inst(item.cls) for items in value.items for item in items if isinstance(item, ClassObj))
    return frozenset(found)


def _try(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Try)
    before = _snapshot(w)
    w.body(node.body)
    after_body = w.frame.names
    ends: List[Env] = []
    for handler in node.handlers:
        w.frame.names = _merge(before, after_body)
        types = w.ev(handler.type) if handler.type is not None else EMPTY
        if handler.name:
            w.parents.append(handler)
            w.bind_name(handler.name, _exception_instances(types), handler)
            w.parents.pop()
        w.body(handler.body)
        ends.append(w.frame.names)
    w.frame.names = after_body
    w.body(node.orelse)
    w.frame.names = _merge(w.frame.names, *ends)
    w.body(node.finalbody)


def _return(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Return)
    if node.value is not None:
        w.returns.append(w.ev(node.value))


def _test(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Assert)
    w.protocol(w.ev(node.test), TRUTH, node.test)
    if node.msg is not None:
        w.ev(node.msg)


def _delete(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Delete)
    for target in node.targets:
        w.parents.append(target)
        if isinstance(target, ast.Name):
            w.delete_name(target.id, target)
        elif isinstance(target, ast.Attribute):
            w.ev(target.value)
        elif isinstance(target, ast.Subscript):
            receivers = w.ev(target.value)
            key = w.ev(subscript_index(target))
            w.protocol(receivers, (SUBSCRIPT_MEMBERS[ast.Del],), target, (key,))
            calls.hash_keys(w, receivers, key, target)
        w.parents.pop()


def _import(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Import)
    index = w.program.index
    for alias in node.names:
        bound = alias.asname or alias.name.split(".")[0]
        target = alias.name if alias.asname else bound
        w.bind_name(bound, w.program.symbol_values(index.imported_module(target)), node, assignment=False)


def _import_from(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.ImportFrom)
    base = w.module.import_base(node)
    for alias in node.names:
        if alias.name != "*":
            symbols = w.program.index.imported_symbols(base, alias.name)
            w.bind_name(alias.asname or alias.name, w.imported_values(symbols), node, assignment=False)


def _function_def(w: Walker, node: ast.AST) -> None:
    """装饰器从内到外依次应用：透明装饰器不改变值；仓库装饰器按一次调用传播；其余记为交给装饰器。"""
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    function = w.program.functions[id(node)]
    decorators = [w.ev(decorator) for decorator in node.decorator_list]
    calls.write_defaults(w, function)
    value: Values = frozenset({Func(function.qual)})
    for decorator, callees in reversed(list(zip(node.decorator_list, decorators))):
        if w.program.is_transparent(function, decorator):
            continue
        repository = frozenset(callee for callee in callees if isinstance(callee, (Func, Method, ClassObj, Inst)))
        if not repository:
            w.record(DECORATOR, function.qual, decorator)
            continue
        value = calls.invoke(w, repository, *calls.library_call(decorator, (value,)))
    w.bind_name(node.name, value, node, assignment=False)


def _class_def(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.ClassDef)
    cls = w.program.class_by_node.get(id(node))
    for part in [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]:
        w.ev(part)
    w.frames.append(Frame(w.module.scopes[id(node)], w.frame.source_qual, cls=cls))
    w.body(node.body)
    w.frames.pop()
    w.bind_name(node.name, frozenset({ClassObj(cls.cid)}) if cls is not None else EMPTY, node, assignment=False)


def _raise(w: Walker, node: ast.AST) -> None:
    assert isinstance(node, ast.Raise)
    for part in (node.exc, node.cause):
        if part is not None:
            w.ev(part)


def _nothing(w: Walker, node: ast.AST) -> None:
    return None


STATEMENTS: Dict[type, Handler] = {
    ast.Expr: _expr,
    ast.Assign: _assign,
    ast.AnnAssign: _ann_assign,
    ast.AugAssign: _aug_assign,
    ast.For: _for,
    ast.AsyncFor: _for,
    ast.While: _while,
    ast.If: _if,
    ast.With: _with,
    ast.AsyncWith: _with,
    ast.Try: _try,
    ast.Return: _return,
    ast.Assert: _test,
    ast.Delete: _delete,
    ast.Import: _import,
    ast.ImportFrom: _import_from,
    ast.FunctionDef: _function_def,
    ast.AsyncFunctionDef: _function_def,
    ast.ClassDef: _class_def,
    ast.Raise: _raise,
    ast.Global: _nothing,
    ast.Nonlocal: _nothing,
    ast.Pass: _nothing,
    ast.Break: _nothing,
    ast.Continue: _nothing,
}
