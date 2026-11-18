"""实参绑定器：把一次调用的实参按 Python 规则对到被调函数的形参上。

类型传播与公开引用索引共用这一个绑定器。调用处出现 ``*args`` / ``**kwargs``、被调函数带有会改写签名的
装饰器、或实参对不上形参（真实运行会抛 TypeError）时，绑定记为不可判定：能确定的那部分照常给出，
消费方不得据此下“恒定”结论。

调用处的 ``*x`` 展开成个数不定的位置实参：一个位置实参（含 ``*x`` 本身）前面有 k 个普通位置实参时，它最早
落在第 k 个位置形参上。前面没有 ``*x`` 时落点就是这一个；出现过 ``*x`` 以后，它可能落在第 k 个及以后的每个
位置形参或 ``*args`` 上，arguments 里同一形参因此可以出现多次，每项是一种可能。k 已越过全部位置形参时，
它确定落进 ``*args``。``*x`` 的实参取值是它展开出的元素。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Signature:
    positional: Tuple[str, ...]
    posonly_count: int
    vararg: Optional[str]
    kwonly: Tuple[str, ...]
    kwarg: Optional[str]
    defaults: Dict[str, ast.expr]
    exact: bool


@dataclass(frozen=True)
class Binding:
    decidable: bool
    receiver: Optional[str]
    arguments: Tuple[Tuple[str, ast.expr], ...]
    defaulted: Tuple[str, ...]
    var_positional: Tuple[ast.expr, ...]
    var_keyword: Tuple[Tuple[str, ast.expr], ...]


def signature_of(args: ast.arguments, *, exact: bool) -> Signature:
    positional_args = [*args.posonlyargs, *args.args]
    defaults: Dict[str, ast.expr] = {}
    for arg, default in zip(positional_args[len(positional_args) - len(args.defaults):], args.defaults):
        defaults[arg.arg] = default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            defaults[arg.arg] = default
    return Signature(
        positional=tuple(arg.arg for arg in positional_args),
        posonly_count=len(args.posonlyargs),
        vararg=args.vararg.arg if args.vararg is not None else None,
        kwonly=tuple(arg.arg for arg in args.kwonlyargs),
        kwarg=args.kwarg.arg if args.kwarg is not None else None,
        defaults=defaults,
        exact=exact,
    )


def field_signature(fields: Sequence[Tuple[str, Optional[ast.expr]]]) -> Signature:
    """dataclass / NamedTuple 生成的构造签名：第一个形参是绑定的 self/cls，其后按字段顺序。"""
    defaults = {name: default for name, default in fields if default is not None}
    return Signature(
        positional=("self", *(name for name, _default in fields)),
        posonly_count=0,
        vararg=None,
        kwonly=(),
        kwarg=None,
        defaults=defaults,
        exact=True,
    )


def bind(signature: Signature, args: Sequence[ast.expr], keywords: Sequence[ast.keyword], *, bound: bool) -> Binding:
    """bound=True 表示第一个形参由接收者隐式绑定（实例方法经实例调用、类方法、构造器）。"""
    positional = list(signature.positional)
    receiver = None
    if bound and positional:
        receiver = positional.pop(0)
    decidable = signature.exact and not (bound and receiver is None)
    assigned: Dict[str, List[ast.expr]] = {}
    var_positional: List[ast.expr] = []
    plain = 0  # 已经过的普通位置实参个数，即当前实参最早落在的位置
    spread = False  # 已出现 *x：之后的落点不定
    for arg in args:
        starred = isinstance(arg, ast.Starred)
        spread = spread or starred
        if starred:
            decidable = False
        if plain < len(positional):
            for name in positional[plain:] if spread else positional[plain:plain + 1]:
                assigned.setdefault(name, []).append(arg)
            if spread and signature.vararg is not None:
                var_positional.append(arg)
        elif signature.vararg is not None:
            var_positional.append(arg)
        else:
            decidable = False
        if not starred:
            plain += 1
    keyword_only = set(signature.kwonly)
    by_keyword = set(positional[max(0, signature.posonly_count - (1 if receiver else 0)):]) | keyword_only
    var_keyword: List[Tuple[str, ast.expr]] = []
    for keyword in keywords:
        if keyword.arg is None:
            decidable = False
        elif keyword.arg in assigned and not spread:
            decidable = False
        elif keyword.arg in by_keyword:
            assigned.setdefault(keyword.arg, []).append(keyword.value)
        elif signature.kwarg is not None:
            var_keyword.append((keyword.arg, keyword.value))
        else:
            decidable = False
    names = [*positional, *signature.kwonly]
    missing = [name for name in names if name not in assigned and name not in signature.defaults]
    if missing:
        decidable = False
    return Binding(
        decidable=decidable,
        receiver=receiver,
        arguments=tuple((name, expr) for name in names for expr in assigned.get(name, ())),
        defaulted=tuple(name for name in names if name not in assigned and name in signature.defaults),
        var_positional=tuple(var_positional),
        var_keyword=tuple(var_keyword),
    )
