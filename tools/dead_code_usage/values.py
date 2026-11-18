"""类型传播里的抽象值。

一个表达式求值得到一组抽象值（``Values``，不可变集合）；空集合表示不知道或不关心（字面常量不跟踪）。
仓库外的类的实例只记类名（``ExternalInst``），供外部类型身份规则使用；它的属性与成员不跟踪。
容器分两种：注解给出的结构容器与按已有元素构造的副本（``SeqT``/``MapT``/``Tup``）是只读的；字面量、推导式与
空构造（不带实参的 ``list()``/``set()``/``dict()``、dataclass 默认工厂）按出现位置建 ``Site``，``append``/下标写入
等改写把元素类型记到该位置上，读取方随之更新。
传播不区分调用上下文：通用 helper 会把各调用方传入的字面量容器合到一起，一组取值里字面量位置超过
``MAX_SITES`` 个时拓宽为 ``MANY_SITES``，它的元素不再跟踪。拓宽是欠近似，而且结果取决于遍历次序：
拓宽之前已经读出并传走的元素不会撤回。求解按稳定次序进行（``solver``），同一份代码的结果固定；
一处改动可能改变共用通用 helper 的函数里拓宽发生的先后，让它们的证据增减。
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import AbstractSet, Dict, FrozenSet, List, Tuple, Union

from .protocols import ExternalSymbols

MAX_DEPTH = 3


@dataclass(frozen=True)
class Inst:
    """仓库类的实例；cls 是类标识 ``rel::ClassName``。"""

    cls: str


@dataclass(frozen=True)
class ClassObj:
    """仓库类对象本身（类名、``cls``、``Type[X]``）。"""

    cls: str


@dataclass(frozen=True)
class Func:
    """函数值：模块函数、嵌套函数、lambda、静态方法，或经类对象取到的未绑定方法。"""

    qual: str


@dataclass(frozen=True)
class Method:
    """绑定方法：receiver 是绑定的实例或类对象（类方法）。"""

    qual: str
    receiver: Value


@dataclass(frozen=True)
class ModuleRef:
    name: str


@dataclass(frozen=True)
class ExternalRef:
    """仓库外的符号（含 ``builtins.*``）；name 是 ``protocols`` 给出的统一身份：同一个真实模块或类只有一个。"""

    name: str


@dataclass(frozen=True)
class ExternalInst:
    """仓库外的类的实例（以外部类作注解，或调用外部类得到）；symbol 是该类的外部符号，只由 ``external_instance`` 构造。

    它的真实继承链在用到类型身份时从已安装的库读取（``protocols.ExternalSymbols.mro``）；它的成员不跟踪。
    """

    symbol: str


@dataclass(frozen=True)
class SeqT:
    elems: FrozenSet[Value]


@dataclass(frozen=True)
class MapT:
    keys: FrozenSet[Value]
    values: FrozenSet[Value]


@dataclass(frozen=True)
class Tup:
    items: Tuple[FrozenSet[Value], ...]


@dataclass(frozen=True)
class Site:
    """字面量或推导式在源码里的位置；kind 是 list / set / dict。"""

    rel: str
    line: int
    col: int
    kind: str


@dataclass(frozen=True)
class SuperV:
    """``super()``：owner 是写下它的类，查找从 owner 之后的 MRO 开始。"""

    owner: str
    receivers: FrozenSet[Value]


@dataclass(frozen=True)
class CallableT:
    """``Callable[..., R]`` 注解：调用得到 returns。"""

    returns: FrozenSet[Value]


@dataclass(frozen=True)
class ManySites:
    """拓宽结果：一组取值里的字面量位置超过 MAX_SITES 个时，这些位置合成这一个“若干字面量容器”。

    它吸收此后流入的字面量位置（保证传播收敛）；读写它的元素什么也得不到、什么也不记。
    """


Value = Union[
    Inst, ClassObj, Func, Method, ModuleRef, ExternalRef, ExternalInst, SeqT, MapT, Tup, Site, SuperV, CallableT, ManySites,
]
Values = FrozenSet[Value]
EMPTY: Values = frozenset()
MANY_SITES = ManySites()
MAX_SITES = 8
_STRUCTURAL = frozenset({SeqT, MapT, Tup, CallableT})


def external_instance(externals: ExternalSymbols, symbol: str) -> Values:
    """外部符号 symbol 的实例：它在本解释器里是可确认的类时给 ``ExternalInst``，否则类型未知（空）。"""
    return frozenset({ExternalInst(symbol)}) if externals.mro(symbol) else EMPTY


def with_members(values: Values) -> Values:
    """会跟踪成员的那部分取值：仓库外的值不跟踪成员，和“不知道”一样不参与成员解析。"""
    return frozenset(value for value in values if not isinstance(value, ExternalInst))


def join(*groups: AbstractSet[Value]) -> Values:
    """合并多组值：结构容器按种类合并元素（元组按长度逐位合并），嵌套超过 MAX_DEPTH 层的内容丢弃，
    字面量位置过多时拓宽为 MANY_SITES。只有一组时原样返回，写入传播状态时由 normalize 收口。"""
    present = [group for group in groups if group]
    if not present:
        return EMPTY
    if len(present) == 1 and isinstance(present[0], frozenset):
        return present[0]
    return _normalize(frozenset().union(*present), MAX_DEPTH)


def normalize(values: Values) -> Values:
    return _normalize(values, MAX_DEPTH)


def _normalize(union: Values, depth: int) -> Values:
    structural = []
    sites = []
    for value in union:
        kind = type(value)
        if kind in _STRUCTURAL:
            structural.append(value)
        elif kind is Site:
            sites.append(value)
    if structural:
        parts = frozenset(structural)
        union = (union - parts) | _merge_structural(parts, depth)
    if sites and (MANY_SITES in union or len(sites) > MAX_SITES):
        union = (union - frozenset(sites)) | {MANY_SITES}
    return union


@functools.lru_cache(maxsize=1 << 16)
def _merge_structural(values: FrozenSet[Value], depth: int) -> Values:
    """同种结构容器合成一个；同一组输入反复出现（每次重新遍历都会），结果按输入缓存。"""
    if depth <= 0:
        return EMPTY
    seq: List[Values] = []
    map_keys: List[Values] = []
    map_values: List[Values] = []
    tuples: Dict[int, List[Tuple[Values, ...]]] = {}
    callables: List[Values] = []
    for value in values:
        if isinstance(value, SeqT):
            seq.append(value.elems)
        elif isinstance(value, MapT):
            map_keys.append(value.keys)
            map_values.append(value.values)
        elif isinstance(value, Tup):
            tuples.setdefault(len(value.items), []).append(value.items)
        elif isinstance(value, CallableT):
            callables.append(value.returns)
    merged: List[Value] = []
    if seq:
        merged.append(SeqT(_capped(seq, depth)))
    if map_keys:
        merged.append(MapT(_capped(map_keys, depth), _capped(map_values, depth)))
    for length, rows in tuples.items():
        merged.append(Tup(tuple(_capped([row[index] for row in rows], depth) for index in range(length))))
    if callables:
        merged.append(CallableT(_capped(callables, depth)))
    return frozenset(merged)


def _capped(groups: List[Values], depth: int) -> Values:
    """合并下一层的值，下一层的结构容器再少一层深度。"""
    return _normalize(frozenset().union(*groups), depth - 1)
