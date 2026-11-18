"""单元遍历：按语句顺序给表达式求值，记下引用，并把类型写回传播状态。

局部名按赋值覆盖（分支结束时合并各分支），字段、模块级名字与闭包读到的外层局部名按全部赋值合并。
lambda、推导式与类体在所在单元里内联遍历，各有一层名字帧；嵌套 def 是独立单元，经闭包键读外层名字。
"""
from __future__ import annotations

import ast
from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from . import calls, exprs, stmts
from .ast_nodes import LOCATED, node_span
from .binder import Binding, bind
from .frames import Frame, param_names
from .imports import DEF, VAR, ModuleInfo, Symbol
from .model import DELETE, LOAD, PROPERTY, PROTOCOL, REFERENCE, STORE, AttributeSite, Reference
from .program import BUILTIN_NAMES, CLASSMETHOD, GETTER, LAMBDA, STATIC, FunctionInfo
from .protocols import (
    COMPARE_OPERATORS,
    CONTAINER_PROTOCOLS,
    FORWARD,
    HASH,
    Chain,
    IteratorProtocol,
)
from .scope import CLASS, MODULE, Scope
from .solver import Solver, Unit, field_key, global_key, local_key, param_key, return_key
from .values import (
    EMPTY,
    ClassObj,
    ExternalRef,
    Func,
    Inst,
    MapT,
    Method,
    ModuleRef,
    SeqT,
    Site,
    SuperV,
    Tup,
    Value,
    Values,
    join,
    with_members,
)


class Walker:
    def __init__(self, solver: Solver, unit: Unit, origin: str) -> None:
        """origin 是这次遍历记下的证据的来源（``Reference.origin``）。"""
        self.solver = solver
        self.origin = origin
        self.program = solver.program
        self.unit = unit
        self.module: ModuleInfo = unit if isinstance(unit, ModuleInfo) else unit.module
        self.rel = self.module.rel
        self.frames: List[Frame] = []
        self.refs: List[Reference] = []
        self.unresolved: List[AttributeSite] = []
        self.returns: List[Values] = []
        self.yields: List[Values] = []
        self.assigned: Dict[str, List[Values]] = defaultdict(list)
        self.parents: List[ast.AST] = []
        self.statement_base = 0

    # --- 入口 -------------------------------------------------------------------------------------------

    def run(self) -> None:
        if isinstance(self.unit, ModuleInfo):
            self.frames.append(Frame(self.module.scope, "<module>"))
            self.body(self.module.tree.body)
            return
        function = self.unit
        frame = Frame(function.scope, function.qual, function=function)
        self.frames.append(frame)
        for name in param_names(function):
            frame.names[name] = self.solver.read(param_key(function.qual, name))
            self.assigned[name].append(frame.names[name])
        node = function.node
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        self.body(node.body)
        result = join(*self.returns)
        if function.is_generator:
            result = join(result, frozenset({SeqT(join(*self.yields))}))
        self.solver.write(return_key(function.qual), result)
        for name, values in self.assigned.items():
            self.solver.write(local_key(function.qual, name), join(*values))

    @property
    def frame(self) -> Frame:
        return self.frames[-1]

    def body(self, nodes: Sequence[ast.stmt]) -> None:
        for node in nodes:
            self.stmt(node)

    def stmt(self, node: ast.stmt) -> None:
        saved = self.statement_base
        self.statement_base = len(self.parents)
        self.parents.append(node)
        handler = stmts.STATEMENTS.get(type(node))
        if handler is not None:
            handler(self, node)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self.ev(child)
        self.parents.pop()
        self.statement_base = saved

    def ev(self, node: ast.AST, callee: bool = False) -> Values:
        """求值；callee=True 表示它正被直接调用，取到函数不算逃逸。"""
        self.parents.append(node)
        handler = exprs.EXPRESSIONS.get(type(node))
        if handler is not None:
            result = handler(self, node, callee)
        else:
            for child in ast.iter_child_nodes(node):
                self.ev(child)
            result = EMPTY
        self.parents.pop()
        return result

    # --- 名字 -------------------------------------------------------------------------------------------

    def load_name(self, name: str, node: ast.Name, callee: bool) -> Values:
        scope = self.frame.scope.binding_scope(name)
        if scope is None:
            return frozenset({ExternalRef(self.program.index.externals.builtin(name))}) if name in BUILTIN_NAMES else EMPTY
        if scope.kind == MODULE:
            self.record_variable_loads(self.program.index.module_symbols(self.module.name, name), node)
        values = self._scope_values(scope, name)
        if not callee:
            self.record_escape(values, node)
        return values

    def _scope_values(self, scope: Scope, name: str) -> Values:
        for frame in reversed(self.frames):
            if frame.scope is scope:
                return frame.names.get(name, EMPTY)
        if scope.kind == MODULE:
            return self.solver.read(global_key(self.rel, name))
        if scope.kind == CLASS:
            return EMPTY
        return self.solver.read(local_key(self.program.functions[id(scope.node)].qual, name))

    def bind_name(self, name: str, values: Values, node: ast.AST, *, assignment: bool = True) -> None:
        """写名字。模块级变量的每次重新绑定都记一次写入，含 import、def、class。assignment=False 用于
        import、def、class 绑定：类体里的它们不当字段值（方法走类的方法表）。"""
        scope = self.frame.scope.binding_scope(name)
        assert scope is not None
        if scope.kind == MODULE:
            self.record_variable_write(self.module, name, STORE, node)
            self.solver.write(global_key(self.rel, name), values)
        for frame in reversed(self.frames):
            if frame.scope is scope:
                frame.names[name] = values
                if frame.cls is not None and assignment:
                    self.solver.write(field_key(frame.cls.cid, name), values)
                if frame is self.frames[0] and frame.function is not None:
                    self.assigned[name].append(values)
                return
        if scope.kind not in (MODULE, CLASS):
            self.solver.write(local_key(self.program.functions[id(scope.node)].qual, name), values)

    def delete_name(self, name: str, node: ast.Name) -> None:
        scope = self.frame.scope.binding_scope(name)
        if scope is not None and scope.kind == MODULE:
            self.record_variable_write(self.module, name, DELETE, node)
        if scope is self.frame.scope:
            self.frame.names.pop(name, None)

    def method_frame(self) -> Optional[Frame]:
        """最内层“类的直接方法”帧（零参 super() 用它的类与第一个形参）。"""
        for frame in reversed(self.frames):
            if frame.function is not None and frame.function.owner is not None:
                return frame
        return None

    # --- 属性 -------------------------------------------------------------------------------------------

    def attribute_load(self, receivers: Values, name: str, node: ast.AST, callee: bool) -> Values:
        """接收者为空或其中任一个解析不出该属性，这次访问记为未解析（可能另有没解析到的调用）。"""
        found: List[Values] = []
        tracked = with_members(receivers)
        resolved = bool(tracked)
        for receiver in tracked:
            if isinstance(receiver, Inst):
                values = self._instance_attribute(receiver, name, node)
            elif isinstance(receiver, ClassObj):
                values = self._class_attribute(receiver, name, node)
            elif isinstance(receiver, ModuleRef):
                values = self.module_attribute(receiver.name, name, node)
            elif isinstance(receiver, ExternalRef):
                # 模块与类的属性仍是外部符号；别的外部对象与读不到的名字的属性是值，与外部实例的成员一样不跟踪。
                symbol = self.program.index.externals.attribute(receiver.name, name)
                values = None if symbol is None else frozenset({ExternalRef(symbol)})
            elif isinstance(receiver, SuperV):
                values = self._super_attribute(receiver, name, node)
            else:
                values = None
            if values is None:
                resolved = False
            else:
                found.append(values)
        if not resolved:
            self.unresolved.append(self.attribute_site(name, node))
        result = join(*found)
        if not callee:
            self.record_escape(result, node)
        return result

    def _instance_attribute(self, receiver: Inst, name: str, node: ast.AST) -> Optional[Values]:
        program = self.program
        function = program.lookup(receiver.cls, name)
        found = [self.solver.read(field_key(owner, name)) for owner in program.classes[receiver.cls].mro]
        if function is not None:
            found.append(self._member_value(function, receiver, node))
        elif not any(found):
            getter = None if name.startswith("__") else program.lookup(receiver.cls, "__getattr__")
            if getter is None:
                return None
            found.append(self.call_member(PROTOCOL, getter, receiver, node, detail="__getattr__"))
        return join(*found)

    def _class_attribute(self, receiver: ClassObj, name: str, node: ast.AST) -> Optional[Values]:
        program = self.program
        function = program.lookup(receiver.cls, name)
        found = [self.solver.read(field_key(owner, name)) for owner in program.classes[receiver.cls].mro]
        if function is not None and function.kind != GETTER:
            found.append(self._member_value(function, receiver, node))
        elif function is None and not any(found):
            return None
        return join(*found)

    def _super_attribute(self, receiver: SuperV, name: str, node: ast.AST) -> Values:
        found = []
        for value in receiver.receivers:
            if isinstance(value, (Inst, ClassObj)):
                function = self.program.lookup(value.cls, name, start_after=receiver.owner)
                if function is not None:
                    found.append(self._member_value(function, value, node))
        return join(*found)

    def _member_value(self, function: FunctionInfo, receiver: Value, node: ast.AST) -> Values:
        """按方法种类取成员：property 记读取并给返回值，其余给函数或绑定方法。"""
        if function.kind == GETTER:
            return self.call_member(PROPERTY, function, receiver, node)
        if function.kind == STATIC:
            return frozenset({Func(function.qual)})
        if function.kind == CLASSMETHOD:
            assert isinstance(receiver, (Inst, ClassObj))
            return frozenset({Method(function.qual, ClassObj(receiver.cls))})
        if isinstance(receiver, ClassObj):
            return frozenset({Func(function.qual)})
        return frozenset({Method(function.qual, receiver)})

    def module_attribute(self, module_name: str, name: str, node: ast.AST) -> Values:
        index = self.program.index
        symbols = index.module_symbols(module_name, name)
        self.record_variable_loads(symbols, node)
        info = index.by_name.get(module_name)
        if info is not None and name in info.top_bindings:
            return self.solver.read(global_key(info.rel, name))
        return self.program.symbol_values(symbols)

    def imported_values(self, symbols: Sequence[Symbol]) -> Values:
        """import 绑定到的值：仓库函数与模块级变量取传播结果（含装饰后的值），其余取静态值。"""
        found = [self.program.symbol_values(symbols)]
        for symbol in symbols:
            if symbol.kind in (DEF, VAR):
                found.append(self.solver.read(global_key(self.program.index.by_name[symbol.module].rel, symbol.name)))
        return join(*found)

    def attribute_store(self, receivers: Values, name: str, values: Values, node: ast.AST) -> None:
        for receiver in receivers:
            if isinstance(receiver, Inst):
                setter = self._setter(receiver.cls, name)
                if setter is None:
                    self.solver.write(field_key(receiver.cls, name), values)
                    continue
                self.call_member(PROPERTY, setter, receiver, node, (values,))
            elif isinstance(receiver, ClassObj):
                self.solver.write(field_key(receiver.cls, name), values)
            elif isinstance(receiver, ModuleRef):
                info = self.program.index.by_name.get(receiver.name)
                if info is not None:
                    self.record_variable_write(info, name, STORE, node)
                    self.solver.write(global_key(info.rel, name), values)
        calls.external_slot_store(self, receivers, name, values, node)

    def _setter(self, cid: str, name: str) -> Optional[FunctionInfo]:
        for owner in self.program.classes[cid].mro:
            setter = self.program.classes[owner].setters.get(name)
            if setter is not None:
                return setter
        return None

    def attribute_site(self, name: str, node: ast.AST) -> AttributeSite:
        assert isinstance(node, LOCATED)
        return AttributeSite(name, self.rel, node.lineno, node.col_offset, self.frame.source_qual, self.origin)

    # --- 绑定目标 ---------------------------------------------------------------------------------------

    def bind(self, target: ast.AST, values: Values) -> None:
        self.parents.append(target)
        if isinstance(target, ast.Name):
            self.bind_name(target.id, values, target)
        elif isinstance(target, (ast.Tuple, ast.List)):
            self._unpack(target.elts, values, target)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, values)
        elif isinstance(target, ast.Attribute):
            self.attribute_store(self.ev(target.value), target.attr, values, target)
        elif isinstance(target, ast.Subscript):
            calls.subscript_store(self, self.ev(target.value), target, values)
        self.parents.pop()

    def _unpack(self, targets: Sequence[ast.expr], values: Values, node: ast.AST) -> None:
        """按位置拆元组（各返回分支已逐位合并）；其他可迭代值每个目标都拿元素类型。"""
        star = next((index for index, target in enumerate(targets) if isinstance(target, ast.Starred)), None)
        slots: List[List[Values]] = [[] for _ in targets]
        for value in values:
            if isinstance(value, Tup) and star is None and len(value.items) == len(targets):
                for index, item in enumerate(value.items):
                    slots[index].append(item)
            elif isinstance(value, Tup) and star is not None and len(value.items) >= len(targets) - 1:
                tail = len(targets) - star - 1
                for index in range(star):
                    slots[index].append(value.items[index])
                slots[star].append(frozenset({SeqT(join(*value.items[star:len(value.items) - tail]))}))
                for offset in range(tail):
                    slots[star + 1 + offset].append(value.items[len(value.items) - tail + offset])
            else:
                elements = self.iterate(frozenset({value}), node)
                for index in range(len(targets)):
                    slots[index].append(frozenset({SeqT(elements)}) if index == star else elements)
        for target, parts in zip(targets, slots):
            self.bind(target, join(*parts))

    # --- 记录 -------------------------------------------------------------------------------------------

    def record(self, kind: str, target: str, node: ast.AST, *, binding: Optional[Binding] = None, detail: str = "") -> None:
        line, col, end_line, end_col = node_span(node)
        self.refs.append(Reference(
            target, self.rel, line, col, end_line, end_col, self.frame.source_qual, kind, detail, self.origin, binding,
        ))

    def record_escape(self, values: Values, node: ast.AST) -> None:
        for value in values:
            if isinstance(value, (Func, Method)) and self.program.by_qual[value.qual].kind != LAMBDA:
                self.record(REFERENCE, value.qual, node)

    def record_variable_loads(self, symbols: Sequence[Symbol], node: ast.AST) -> None:
        """读取沿 import 链落到的模块级变量。"""
        for symbol in symbols:
            if symbol.kind == VAR:
                self._record_variable(self.program.index.by_name[symbol.module].rel, symbol.name, LOAD, node)

    def record_variable_write(self, module: ModuleInfo, name: str, kind: str, node: ast.AST) -> None:
        """写入或删除模块 module 自己的全局名；它在该模块里有赋值绑定时才是模块级变量。"""
        if any(binding.kind == VAR for binding in module.top_bindings.get(name, ())):
            self._record_variable(module.rel, name, kind, node)

    def _record_variable(self, rel: str, name: str, kind: str, node: ast.AST) -> None:
        line, col, end_line, end_col = node_span(node)
        context = tuple(reversed(self.parents[self.statement_base:-1]))
        self.refs.append(Reference(
            f"{rel}::{name}", self.rel, line, col, end_line, end_col, self.frame.source_qual, kind, "", self.origin,
            None, context,
        ))

    # --- 协议与迭代 -------------------------------------------------------------------------------------

    def call_member(self, kind: str, function: FunctionInfo, receiver: Value, node: ast.AST,
                    operands: Sequence[Values] = (), detail: str = "") -> Values:
        """解释器或库在 node 处隐式调用方法（协议成员、property 的 getter / setter）：接收者与按位置交给它的操作数
        经实参绑定写进形参，记一条 kind 引用，返回方法的返回值。"""
        call, argvals = calls.library_call(node, operands)
        assert function.signature is not None
        binding = bind(function.signature, call.args, call.keywords, bound=True)
        self.record(kind, function.qual, node, detail=detail)
        calls.pass_arguments(self, function, binding, argvals, receiver)
        return self.solver.read(return_key(function.qual))

    def protocol(self, values: Values, chain: Chain, node: ast.AST, operands: Sequence[Values] = ()) -> Values:
        """对值里每个仓库实例按链取第一个有定义的协议成员，以 operands 为位置实参调用它，记证据并返回它的返回值。
        内置容器自己实现的成员对元素再做的协议调用（``CONTAINER_PROTOCOLS``）在这里一并展开。"""
        found = []
        for value in values:
            if not isinstance(value, Inst):
                continue
            for name in chain:
                function = self.program.lookup(value.cls, name)
                if function is not None:
                    found.append(self.call_member(PROTOCOL, function, value, node, operands, name))
                    break
        element_chain = CONTAINER_PROTOCOLS.get(chain)
        if element_chain is not None:
            contained = self._contained(values, element_chain == HASH)
            if contained:
                self.protocol(contained, element_chain, node)
        return join(*found)

    def _contained(self, values: Values, hashing: bool) -> Values:
        """values 里的内置容器逐层展开后的非容器元素。取哈希时只有元组与 frozenset（序列值）可哈希，只展开它们；
        文本转换展开全部内置容器，字典取键与值。同一容器只展开一次，含有自己的列表也能展开完。"""
        pending = list(values)
        seen = set()
        while pending:
            value = pending.pop()
            if isinstance(value, Tup):
                items = join(*value.items)
            elif isinstance(value, SeqT):
                items = value.elems
            elif hashing:
                continue
            elif isinstance(value, Site) and value.kind == "dict":
                items = join(self.solver.read(("keys", value)), self.solver.read(("values", value)))
            elif isinstance(value, Site):
                items = self.solver.read(("elems", value))
            elif isinstance(value, MapT):
                items = join(value.keys, value.values)
            else:
                continue
            pending.extend(items - seen)
            seen.update(items)
        return frozenset(value for value in seen if not isinstance(value, (Tup, SeqT, Site, MapT)))

    def binary(self, left: Values, chain: Chain, right: Values, reflected: str, node: ast.AST) -> Values:
        """二元运算与增量赋值按 Python 的派发规则：左操作数取 chain 里第一个有定义的方法，以右操作数为实参；右操作数的
        反射方法以左操作数为实参，只在左值未知、或左右类型不同时才会被调用。同类型的运算不试反射（左边缺正向方法或
        返回 NotImplemented 都抛 TypeError）。返回被调方法的返回值。"""
        found = [self.protocol(left, chain, node, (right,))]
        for value in right:
            if isinstance(value, Inst) and (not left or any(other != value for other in left)):
                found.append(self.protocol(frozenset({value}), (reflected,), node, (left,)))
        return join(*found)

    def compare(self, left: Values, operator: type, right: Values, node: ast.AST) -> None:
        """比较 ``left op right``：左操作数取正向链、右操作数取反射链，各以对方为实参（同类型也会试反射）。"""
        left_chain, right_chain = COMPARE_OPERATORS[operator]
        self.protocol(left, left_chain, node, (right,))
        self.protocol(right, right_chain, node, (left,))

    def iterator(self, values: Values, node: ast.AST, protocol: IteratorProtocol = FORWARD) -> Values:
        """取迭代器（``iter(x)``、``reversed(x)``）：仓库实例有协议方法时取它的返回值；没有时得到按下标取元素的
        迭代器，用元素序列表示（旧式序列在这里调用 ``__getitem__``，倒序先取 ``__len__``）；容器值给出元素序列。"""
        method, prepare = protocol
        found: List[Values] = []
        for value in values:
            if isinstance(value, Inst):
                function = self.program.lookup(value.cls, method)
                if function is not None:
                    found.append(self.call_member(PROTOCOL, function, value, node, detail=method))
                    continue
                single = frozenset({value})
                self.protocol(single, prepare, node)
                found.append(frozenset({SeqT(self.protocol(single, ("__getitem__",), node))}))
            else:
                elements = self.elements(value)
                if elements:
                    found.append(frozenset({SeqT(elements)}))
        return join(*found)

    def advance(self, iterators: Values, node: ast.AST) -> Values:
        """推进迭代器（``next``）得到的元素：仓库实例调用 ``__next__``，其余取容器元素。"""
        return join(self.protocol(iterators, ("__next__",), node), *(self.elements(value) for value in iterators))

    def iterate(self, values: Values, node: ast.AST) -> Values:
        """完整迭代（``for``、拆包、消费型 API）得到的元素：先取迭代器，再推进。"""
        return self.advance(self.iterator(values, node), node)

    def contains(self, values: Values, needle: Values, node: ast.AST) -> None:
        """``needle in values``：仓库实例有 ``__contains__`` 时以被测值调用它；其余（没有该方法的实例与容器）完整
        迭代，逐个元素与被测值做 ``==`` 比较。集合与字典先对被测值取哈希（``calls.hash_keys``）。"""
        members = frozenset(
            value for value in values if isinstance(value, Inst) and self.program.lookup(value.cls, "__contains__") is not None
        )
        self.protocol(members, ("__contains__",), node, (needle,))
        calls.hash_keys(self, values, needle, node)
        self.compare(self.iterate(values - members, node), ast.Eq, needle, node)

    def elements(self, value: Value) -> Values:
        """容器值本身的元素（不触发协议）。"""
        if isinstance(value, Site):
            return self.solver.read(("keys" if value.kind == "dict" else "elems", value))
        if isinstance(value, SeqT):
            return value.elems
        if isinstance(value, MapT):
            return value.keys
        if isinstance(value, Tup):
            return join(*value.items)
        return EMPTY
