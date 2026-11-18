"""类型传播的不动点求解：工作表 + 读依赖。

传播状态全部是只增不减的值集合，按键存放：形参（含调用处流入的实参类型）、返回值、实例字段与类属性、
模块级名字、函数局部名（给闭包读）、字面量位置的元素。每个遍历单元（模块体或一个函数体）遍历时登记读过
哪些键；某键变大就把读过它的单元重新排队。队列清空时，每个单元最后一次遍历看到的都是最终值，
它记下的引用就是最终引用。

遍历次序固定，与哈希无关（拓宽让结果依赖次序，见 ``values``）：
- 单元按稳定序号编号：模块按传入文件的顺序在前，函数按定义在源码里出现的顺序在后；队列每次取序号最小的
  单元，与单元何时、以什么顺序入队无关。
- 一次遍历只读遍历开始时的状态，写入先按键汇总，遍历结束时一次合并。单元内部按取值集合迭代的先后
  （随字符串哈希变化）因此不影响读到什么、写进什么。

求解可以分段：一段只把选中的单元入队，到不动点后再入队下一批，在同一份状态上单调继续
（``reference_index`` 先求生产与工具代码，再加入测试代码，以分出两类证据）。
"""
from __future__ import annotations

from collections import defaultdict
from heapq import heappop, heappush
from typing import TYPE_CHECKING, Callable, Dict, Hashable, List, Set, Tuple, Type, Union

from .ast_nodes import FUNCTION_NODES
from .imports import ModuleInfo
from .model import AttributeSite, Reference
from .program import CLASSMETHOD, GETTER, LAMBDA, METHOD, SETTER, STATIC, FunctionInfo, Program
from .type_hints import annotation_values, function_type_comment
from .values import EMPTY, ClassObj, Inst, MapT, SeqT, Values, join, normalize

if TYPE_CHECKING:
    from .walker import Walker

Unit = Union[ModuleInfo, FunctionInfo]
Key = Tuple[Hashable, ...]
MAX_WALKS_PER_UNIT = 1000


def param_key(qual: str, name: str) -> Key:
    return ("param", qual, name)


def return_key(qual: str) -> Key:
    return ("return", qual)


def field_key(cid: str, name: str) -> Key:
    return ("field", cid, name)


def global_key(rel: str, name: str) -> Key:
    return ("global", rel, name)


def local_key(qual: str, name: str) -> Key:
    return ("local", qual, name)


class Solver:
    """单元表在构造时建好（模块在前、函数在后）；lambda 不单独成单元，它在定义处随外层单元一起遍历。"""

    def __init__(self, program: Program) -> None:
        self.program = program
        self.values: Dict[Key, Values] = {}
        self.readers: Dict[Key, Set[int]] = defaultdict(set)
        self.units: List[Unit] = [*program.modules.values()]
        self.units.extend(function for function in program.functions.values() if function.kind != LAMBDA)
        self.queue: List[int] = []
        self.queued: Set[int] = set()
        self.walks: Dict[int, int] = defaultdict(int)
        self.current = 0
        self.refs: Dict[int, List[Reference]] = {}
        self.unresolved: Dict[int, List[AttributeSite]] = {}
        self.pending: Dict[Key, Values] = {}
        self._seed()

    def read(self, key: Key) -> Values:
        self.readers[key].add(self.current)
        return self.values.get(key, EMPTY)

    def write(self, key: Key, values: Values) -> None:
        pending = self.pending.get(key)
        self.pending[key] = values if pending is None else pending | values

    def _flush(self) -> None:
        """遍历结束时合并它的全部写入：变大的键把读过它的单元重新排队。"""
        for key, values in self.pending.items():
            old = self.values.get(key, EMPTY)
            if values <= old:
                continue
            new = normalize(old | values)
            if new != old:
                self.values[key] = new
                for unit in self.readers.get(key, ()):
                    self._enqueue(unit)
        self.pending = {}

    def _enqueue(self, unit: int) -> None:
        if unit not in self.queued:
            self.queued.add(unit)
            heappush(self.queue, unit)

    def solve(self, walker_type: Type[Walker], admit: Callable[[Unit], bool], origin: Callable[[str], str]) -> None:
        """把 admit 选中的单元入队，求解到不动点；这一段里遍历记下的证据按 origin(所在文件) 标来源。

        可以分段求解：后一段在前一段的状态上继续，状态只增不减，只重走读到变大键的单元。
        """
        for unit_id, unit in enumerate(self.units):
            if admit(unit):
                self._enqueue(unit_id)
        while self.queue:
            unit_id = heappop(self.queue)
            self.queued.discard(unit_id)
            self.walks[unit_id] += 1
            unit = self.units[unit_id]
            if self.walks[unit_id] > MAX_WALKS_PER_UNIT:
                name = unit.rel if isinstance(unit, ModuleInfo) else unit.qual
                raise RuntimeError(f"类型传播没有收敛：{name} 已遍历 {MAX_WALKS_PER_UNIT} 次")
            self.current = unit_id
            walker = walker_type(self, unit, origin(unit.rel))
            walker.run()
            self._flush()
            self.refs[unit_id] = walker.refs
            self.unresolved[unit_id] = walker.unresolved

    def evidence(self) -> Tuple[List[Reference], List[AttributeSite]]:
        """当前不动点上各单元最后一次遍历记下的引用与未解析属性访问。"""
        return (
            [reference for unit in self.refs.values() for reference in unit],
            [site for unit in self.unresolved.values() for site in unit],
        )

    def _seed(self) -> None:
        """注解、type comment 与 self/cls 给出的初始类型。"""
        program = self.program
        for function in program.functions.values():
            if function.kind == LAMBDA:
                continue
            signature = function.signature
            assert signature is not None
            owner = function.owner
            if owner is not None and signature.positional:
                if function.kind in (METHOD, GETTER, SETTER):
                    self.values[param_key(function.qual, signature.positional[0])] = frozenset({Inst(owner.cid)})
                elif function.kind == CLASSMETHOD:
                    self.values[param_key(function.qual, signature.positional[0])] = frozenset({ClassObj(owner.cid)})
            outer = function.scope.parent
            assert outer is not None
            args = function.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self._seed_value(param_key(function.qual, arg.arg), annotation_values(program, outer, function.module, arg.annotation))
            if args.vararg is not None and args.vararg.annotation is not None:
                values = annotation_values(program, outer, function.module, args.vararg.annotation)
                self._seed_value(param_key(function.qual, args.vararg.arg), frozenset({SeqT(values)}))
            if args.kwarg is not None and args.kwarg.annotation is not None:
                values = annotation_values(program, outer, function.module, args.kwarg.annotation)
                self._seed_value(param_key(function.qual, args.kwarg.arg), frozenset({MapT(EMPTY, values)}))
            node = function.node
            assert isinstance(node, FUNCTION_NODES)
            self._seed_value(return_key(function.qual), annotation_values(program, outer, function.module, node.returns))
            if node.type_comment:
                self._seed_type_comment(function, node.type_comment)

    def _seed_type_comment(self, function: FunctionInfo, comment: str) -> None:
        signature = function.signature
        assert signature is not None
        outer = function.scope.parent
        assert outer is not None
        parsed = function_type_comment(comment)
        params = list(signature.positional)
        if function.owner is not None and function.kind != STATIC and params:
            params = params[1:]
        for name, annotation in zip(params, parsed.argtypes):
            self._seed_value(param_key(function.qual, name), annotation_values(self.program, outer, function.module, annotation))
        self._seed_value(return_key(function.qual), annotation_values(self.program, outer, function.module, parsed.returns))

    def _seed_value(self, key: Key, values: Values) -> None:
        if values:
            self.values[key] = join(self.values.get(key, EMPTY), values)
