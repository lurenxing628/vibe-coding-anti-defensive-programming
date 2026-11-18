"""表达式求值：名字、属性、下标、运算、字面量、推导式与 lambda。调用见 calls.py。"""
from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from . import calls
from .ast_nodes import constant_int, subscript_index
from .frames import Frame, param_names
from .protocols import (
    BINARY_OPERATORS,
    COMPARE_OPERATORS,
    FORMAT,
    PERCENT_CONVERSIONS,
    REPR,
    SUBSCRIPT_MEMBERS,
    TO_STR,
    TRUTH,
    UNARY_OPERATORS,
    Chain,
)
from .solver import param_key, return_key
from .values import EMPTY, ExternalInst, Func, Inst, MapT, SeqT, Site, Tup, Value, Values, join

if TYPE_CHECKING:
    from .walker import Walker

Handler = Callable[["Walker", ast.AST, bool], Values]
_CONVERSIONS = {ord("s"): TO_STR, ord("r"): REPR, ord("a"): REPR, -1: FORMAT}
# 一个 ``%`` 转换说明：可选的 (键)、标志、宽度、精度、长度修饰，最后是转换符。
_PERCENT_SPEC = re.compile(
    r"%(?:\((?P<key>[^)]*)\))?[-#0 +]*(?P<width>\*|\d*)(?:\.(?P<precision>\*|\d*))?[hlL]?(?P<conversion>.)", re.S,
)


def _name(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Name)
    return w.load_name(node.id, node, callee)


def _attribute(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Attribute)
    return w.attribute_load(w.ev(node.value), node.attr, node, callee)


def _subscript(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Subscript)
    receivers = w.ev(node.value)
    index = subscript_index(node)
    return subscript_load(w, receivers, index, w.ev(index), node)


def subscript_load(w: Walker, receivers: Values, index: ast.AST, key: Values, node: ast.AST) -> Values:
    """``receivers[index]``：仓库实例以下标的取值 key 调用 ``__getitem__``；容器按种类取元素，字典按键查找时对键取哈希。"""
    found: List[Values] = [w.protocol(receivers, (SUBSCRIPT_MEMBERS[ast.Load],), node, (key,))]
    calls.hash_keys(w, receivers, key, node)
    sliced = isinstance(index, ast.Slice)
    for value in receivers:
        if isinstance(value, Site) and value.kind == "dict":
            found.append(calls.dict_read(w, value, calls.constant_key(index)))
        elif isinstance(value, Site):
            found.append(frozenset({value}) if sliced else w.solver.read(("elems", value)))
        elif isinstance(value, SeqT):
            found.append(frozenset({value}) if sliced else value.elems)
        elif isinstance(value, MapT):
            found.append(value.values)
        elif isinstance(value, Tup):
            position = constant_int(index)
            if sliced:
                found.append(frozenset({SeqT(join(*value.items))}))
            elif position is not None and -len(value.items) <= position < len(value.items):
                found.append(value.items[position])
            else:
                found.append(join(*value.items))
    return join(*found)


def _call(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Call)
    return calls.evaluate_call(w, node)


def _bin_op(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.BinOp)
    left = w.ev(node.left)
    right = w.ev(node.right)
    if isinstance(node.op, ast.Mod):
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            # 常量格式串由 str.__mod__ 自己格式化，不会交给右操作数的 __rmod__。
            _percent_format(w, node.left.value, right, node)
            return EMPTY
        if not isinstance(node.left, ast.Constant) and _may_be_text(w, left):
            _percent_format(w, None, right, node)
    return binary_value(w, left, right, node.op, (BINARY_OPERATORS[type(node.op)][0],), node)


def binary_value(w: Walker, left: Values, right: Values, operator: ast.operator, chain: Chain, node: ast.AST) -> Values:
    """``left op right`` 的值：被调运算方法的返回值（``Walker.binary``）；内置序列相加得到新序列。"""
    result = w.binary(left, chain, right, BINARY_OPERATORS[type(operator)][1], node)
    if isinstance(operator, ast.Add):
        sequences = [value for value in left | right if isinstance(value, (Site, SeqT, Tup))]
        if sequences:
            result = join(result, frozenset({SeqT(join(*(w.elements(value) for value in sequences)))}))
    return result


def _may_be_text(w: Walker, values: Values) -> bool:
    """左操作数可能是字符串：类型未知，或含 str 的实例。"""
    return not values or any(
        isinstance(value, ExternalInst) and "builtins.str" in w.program.index.externals.mro(value.symbol)
        for value in values
    )


def _percent_format(w: Walker, template: Optional[str], operands: Values, node: ast.AST) -> None:
    """字符串 ``%`` 格式化按转换符对操作数调用协议成员（``PERCENT_CONVERSIONS``）：元组按位置对应各转换（``*`` 宽度
    与精度各占一个整数实参），映射形式 ``%(键)s`` 按键取值（仓库实例经 ``__getitem__``）；不是元组的单个操作数
    对应第一个转换。格式串不是常量（template 为 None）时每种转换都可能。"""
    if template is None:
        items = join(*(_format_items(value) for value in operands))
        for chain in dict.fromkeys(PERCENT_CONVERSIONS.values()):
            w.protocol(items, chain, node)
        return
    specs = [match for match in _PERCENT_SPEC.finditer(template) if match.group("conversion") != "%"]
    if any(match.group("key") is not None for match in specs):
        for match in specs:
            chain = PERCENT_CONVERSIONS.get(match.group("conversion"))
            if chain is not None:
                w.protocol(_mapped(w, operands, match.group("key"), node), chain, node)
        return
    slots: List[Optional[Chain]] = []
    for match in specs:
        slots.extend(None for part in ("width", "precision") if match.group(part) == "*")
        slots.append(PERCENT_CONVERSIONS.get(match.group("conversion")))
    for value in operands:
        if isinstance(value, Tup):
            pairs = list(zip(slots, value.items))
        elif isinstance(value, SeqT):
            pairs = [(slot, value.elems) for slot in slots]
        else:
            pairs = [(slots[0], frozenset({value}))] if slots else []
        for chain, items in pairs:
            if chain is not None:
                w.protocol(items, chain, node)


def _format_items(value: Value) -> Values:
    """格式串未知时一个操作数可能交出的值：元组与序列的元素，或它本身。"""
    if isinstance(value, Tup):
        return join(*value.items)
    if isinstance(value, SeqT):
        return value.elems
    return frozenset({value})


def _mapped(w: Walker, operands: Values, key: str, node: ast.AST) -> Values:
    """映射形式的格式化按键取到的值。"""
    found: List[Values] = []
    for value in operands:
        if isinstance(value, Site) and value.kind == "dict":
            found.append(calls.dict_read(w, value, key))
        elif isinstance(value, MapT):
            found.append(value.values)
        elif isinstance(value, Inst):
            found.append(w.protocol(frozenset({value}), (SUBSCRIPT_MEMBERS[ast.Load],), node, (EMPTY,)))
    return join(*found)


def _unary_op(w: Walker, node: ast.AST, callee: bool) -> Values:
    """一元运算的值是被调协议方法的返回值；``not`` 只做真值测试，结果是布尔。"""
    assert isinstance(node, ast.UnaryOp)
    result = w.protocol(w.ev(node.operand), UNARY_OPERATORS[type(node.op)], node)
    return EMPTY if isinstance(node.op, ast.Not) else result


def _bool_op(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.BoolOp)
    operands = [w.ev(value) for value in node.values]
    for values in operands[:-1]:
        w.protocol(values, TRUTH, node)
    return join(*operands)


def _compare(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Compare)
    left = w.ev(node.left)
    for operator, comparator in zip(node.ops, node.comparators):
        right = w.ev(comparator)
        if isinstance(operator, (ast.In, ast.NotIn)):
            w.contains(right, left, node)
        elif type(operator) in COMPARE_OPERATORS:
            w.compare(left, type(operator), right, node)
        left = right
    return EMPTY


def _if_exp(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.IfExp)
    w.protocol(w.ev(node.test), TRUTH, node)
    return join(w.ev(node.body, callee), w.ev(node.orelse, callee))


def _joined_str(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.JoinedStr)
    for part in node.values:
        if isinstance(part, ast.FormattedValue):
            w.protocol(w.ev(part.value), _CONVERSIONS[part.conversion], part)
            if part.format_spec is not None:
                w.ev(part.format_spec)
    return EMPTY


def _sequence_items(w: Walker, elts: List[ast.expr]) -> Values:
    found = []
    for element in elts:
        if isinstance(element, ast.Starred):
            found.append(w.iterate(w.ev(element.value), element))
        else:
            found.append(w.ev(element))
    return join(*found)


def _list(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, (ast.List, ast.Set))
    site = calls.new_container(w, node, "set" if isinstance(node, ast.Set) else "list")
    calls.add_elements(w, site, _sequence_items(w, node.elts), node)
    return frozenset({site})


def _tuple(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Tuple)
    if any(isinstance(element, ast.Starred) for element in node.elts):
        return frozenset({SeqT(_sequence_items(w, node.elts))})
    return frozenset({Tup(tuple(w.ev(element) for element in node.elts))})


def _dict(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Dict)
    site = calls.new_container(w, node, "dict")
    for key, value in zip(node.keys, node.values):
        values = w.ev(value)
        if key is None:
            for source in values:
                calls.dict_write(w, site, None, *calls.mapping_items(w, source, node), node)
        else:
            calls.dict_write(w, site, key, w.ev(key), values, node)
    return frozenset({site})


def _comprehension(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
    iterable = w.ev(node.generators[0].iter)
    w.frames.append(Frame(w.module.scopes[id(node)], w.frame.source_qual, function=None))
    for index, generator in enumerate(node.generators):
        if index:
            iterable = w.ev(generator.iter)
        w.bind(generator.target, w.iterate(iterable, generator.iter))
        for condition in generator.ifs:
            w.protocol(w.ev(condition), TRUTH, condition)
    if isinstance(node, ast.DictComp):
        keys, values = w.ev(node.key), w.ev(node.value)
        w.frames.pop()
        site = calls.new_container(w, node, "dict")
        calls.dict_write(w, site, None, keys, values, node)
        return frozenset({site})
    element = w.ev(node.elt)
    w.frames.pop()
    if isinstance(node, ast.GeneratorExp):
        return frozenset({SeqT(element)})
    site = calls.new_container(w, node, "set" if isinstance(node, ast.SetComp) else "list")
    calls.add_elements(w, site, element, node)
    return frozenset({site})


def _lambda(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Lambda)
    function = w.program.functions[id(node)]
    calls.write_defaults(w, function)
    frame = Frame(function.scope, function.qual, function=None)
    for name in param_names(function):
        frame.names[name] = w.solver.read(param_key(function.qual, name))
    w.frames.append(frame)
    result = w.ev(node.body)
    w.frames.pop()
    w.solver.write(return_key(function.qual), result)
    return frozenset({Func(function.qual)})


def _named_expr(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.NamedExpr)
    values = w.ev(node.value)
    w.parents.append(node.target)
    w.bind_name(node.target.id, values, node.target)
    w.parents.pop()
    return values


def _await(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, ast.Await)
    return w.ev(node.value)


def _yield(w: Walker, node: ast.AST, callee: bool) -> Values:
    assert isinstance(node, (ast.Yield, ast.YieldFrom))
    if node.value is not None:
        values = w.ev(node.value)
        w.yields.append(w.iterate(values, node) if isinstance(node, ast.YieldFrom) else values)
    return EMPTY


def _starred(w: Walker, node: ast.AST, callee: bool) -> Values:
    """调用实参里的 ``*x``：展开成 x 的各个元素，取值就是元素（落到哪些形参由 ``binder`` 决定）。"""
    assert isinstance(node, ast.Starred)
    return w.iterate(w.ev(node.value), node)


def _constant(w: Walker, node: ast.AST, callee: bool) -> Values:
    return EMPTY


EXPRESSIONS: Dict[type, Handler] = {
    ast.Name: _name,
    ast.Attribute: _attribute,
    ast.Subscript: _subscript,
    ast.Call: _call,
    ast.BinOp: _bin_op,
    ast.UnaryOp: _unary_op,
    ast.BoolOp: _bool_op,
    ast.Compare: _compare,
    ast.IfExp: _if_exp,
    ast.JoinedStr: _joined_str,
    ast.List: _list,
    ast.Set: _list,
    ast.Tuple: _tuple,
    ast.Dict: _dict,
    ast.ListComp: _comprehension,
    ast.SetComp: _comprehension,
    ast.DictComp: _comprehension,
    ast.GeneratorExp: _comprehension,
    ast.Lambda: _lambda,
    ast.NamedExpr: _named_expr,
    ast.Await: _await,
    ast.Yield: _yield,
    ast.YieldFrom: _yield,
    ast.Starred: _starred,
    ast.Constant: _constant,
}
