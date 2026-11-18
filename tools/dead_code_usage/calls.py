"""调用求值：仓库函数与方法（经实参绑定器传播实参类型）、类构造链、外部 API 表、库调用的外部属性、容器方法、
下标写入。"""
from __future__ import annotations

import ast
import functools
from typing import TYPE_CHECKING, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from .ast_nodes import LOCATED, subscript_index
from .binder import Binding, bind, field_signature
from .model import CALL, CONSTRUCTOR, PROTOCOL
from .program import ClassInfo, FunctionInfo
from .protocols import (
    BACKWARD,
    EXTERNAL_API,
    EXTERNAL_ATTRIBUTE_SLOTS,
    HASH,
    LIST_SORT,
    SUBSCRIPT_MEMBERS,
    TRUTH,
    ExternalApi,
    pytest_hook_names,
)
from .solver import field_key, param_key, return_key
from .type_hints import annotation_values
from .values import (
    EMPTY,
    CallableT,
    ClassObj,
    ExternalInst,
    ExternalRef,
    Func,
    Inst,
    ManySites,
    MapT,
    Method,
    SeqT,
    Site,
    SuperV,
    Tup,
    Value,
    Values,
    external_instance,
    join,
    with_members,
)

if TYPE_CHECKING:
    from .walker import Walker

ArgValues = Dict[int, Values]
_CONTAINERS = (Site, SeqT, MapT, Tup, ManySites)
_LIST_ADD = frozenset({"append", "add", "insert"})
_LIST_EXTEND = frozenset({"extend", "update"})
_MAPPING_LOOKUPS = frozenset({"get", "pop", "setdefault"})
_KEY_LOOKUPS = _MAPPING_LOOKUPS | {"discard", "remove"}
_HASHED_COLLECTIONS = frozenset({"builtins.set", "builtins.frozenset"})


def evaluate_call(w: Walker, node: ast.Call) -> Values:
    func = node.func
    receivers: Optional[Values] = None
    if isinstance(func, ast.Attribute):
        w.parents.append(func)
        receivers = w.ev(func.value)
        w.parents.pop()
    elif isinstance(func, ast.Name) and func.id == "super" and w.frame.scope.binding_scope("super") is None:
        return _super(w, node)
    argvals = _evaluate_arguments(w, node)
    if receivers is None:
        return invoke(w, w.ev(func, callee=True), node, argvals)
    assert isinstance(func, ast.Attribute)
    containers = [value for value in receivers if isinstance(value, _CONTAINERS)]
    found = [container_method(w, value, func.attr, node, argvals) for value in containers]
    if func.attr == "__init__":
        for value in receivers:
            if isinstance(value, SuperV):
                _super_init(w, value, node, argvals)
    if containers and len(containers) == len(with_members(receivers)):
        return join(*found)
    w.parents.append(func)
    callees = w.attribute_load(receivers - frozenset(containers), func.attr, func, callee=True)
    w.parents.pop()
    found.append(invoke(w, callees, node, argvals))
    return join(*found)


def _evaluate_arguments(w: Walker, node: ast.Call) -> ArgValues:
    argvals: ArgValues = {}
    for arg in node.args:
        argvals[id(arg)] = w.ev(arg)
    for keyword in node.keywords:
        argvals[id(keyword.value)] = w.ev(keyword.value)
    return argvals


def invoke(w: Walker, callees: Values, node: ast.Call, argvals: ArgValues) -> Values:
    found: List[Values] = []
    for callee in callees:
        if isinstance(callee, Func):
            found.append(call_function(w, w.program.by_qual[callee.qual], node, argvals, None))
        elif isinstance(callee, Method):
            found.append(call_function(w, w.program.by_qual[callee.qual], node, argvals, callee.receiver))
        elif isinstance(callee, ClassObj):
            found.append(construct(w, callee.cls, node, argvals))
        elif isinstance(callee, Inst):
            function = w.program.lookup(callee.cls, "__call__")
            if function is not None:
                found.append(call_function(w, function, node, argvals, callee))
        elif isinstance(callee, ExternalRef):
            found.append(_external_call(w, callee.name, node, argvals))
        elif isinstance(callee, CallableT):
            found.append(callee.returns)
    return join(*found)


def call_function(w: Walker, function: FunctionInfo, node: ast.Call, argvals: ArgValues,
                  receiver: Optional[Value], kind: str = CALL) -> Values:
    """记一次已解析调用并按绑定结果把实参类型写进形参。"""
    signature = function.signature
    assert signature is not None
    binding = bind(signature, node.args, node.keywords, bound=receiver is not None)
    w.record(kind, function.qual, node, binding=binding)
    pass_arguments(w, function, binding, argvals, receiver)
    return w.solver.read(return_key(function.qual))


def pass_arguments(w: Walker, function: FunctionInfo, binding: Binding, argvals: ArgValues, receiver: Optional[Value]) -> None:
    """按绑定结果把接收者与实参类型写进形参；显式调用与解释器的隐式调用（``Walker.call_member``）共用。"""
    signature = function.signature
    assert signature is not None
    if receiver is not None and binding.receiver is not None:
        w.solver.write(param_key(function.qual, binding.receiver), frozenset({receiver}))
    for name, expr in binding.arguments:
        w.solver.write(param_key(function.qual, name), argvals[id(expr)])
    if binding.var_positional and signature.vararg is not None:
        items = join(*(argvals[id(expr)] for expr in binding.var_positional))
        w.solver.write(param_key(function.qual, signature.vararg), frozenset({SeqT(items)}))
    if binding.var_keyword and signature.kwarg is not None:
        items = join(*(argvals[id(expr)] for _name, expr in binding.var_keyword))
        w.solver.write(param_key(function.qual, signature.kwarg), frozenset({MapT(EMPTY, items)}))


def library_call(node: ast.AST, arguments: Sequence[Values]) -> Tuple[ast.Call, ArgValues]:
    """解释器或库在 node 处发起的调用：合成一个调用节点（位置与跨度取 node），arguments 依次是各位置实参的取值。"""
    args: List[ast.expr] = [ast.copy_location(ast.Name(id="_", ctx=ast.Load()), node) for _values in arguments]
    call = ast.copy_location(ast.Call(func=ast.Name(id="_", ctx=ast.Load()), args=args, keywords=[]), node)
    return call, {id(arg): values for arg, values in zip(args, arguments)}


def construct(w: Walker, cid: str, node: ast.Call, argvals: ArgValues) -> Values:
    """``C(...)``：执行 MRO 上的 __init__（显式定义，或 dataclass/NamedTuple 生成的初始化链）与 __new__。"""
    instance = Inst(cid)
    _initialize(w, cid, instance, node, argvals, None)
    new = w.program.lookup(cid, "__new__")
    if new is not None:
        call_function(w, new, node, argvals, ClassObj(cid), CONSTRUCTOR)
    return frozenset({instance})


def _initialize(w: Walker, cid: str, instance: Inst, node: ast.Call, argvals: ArgValues, start_after: Optional[str]) -> None:
    explicit, generated = w.program.init_provider(cid, start_after)
    if explicit is not None:
        call_function(w, explicit, node, argvals, instance, CONSTRUCTOR if start_after is None else CALL)
    elif generated is not None:
        fields = w.program.init_fields(generated.cid)
        binding = bind(field_signature([(name, default) for name, default, _owner in fields]), node.args, node.keywords, bound=True)
        owners = {name: owner for name, _default, owner in fields}
        for name, expr in binding.arguments:
            w.solver.write(field_key(owners[name], name), argvals[id(expr)])
        _post_init(w, generated, instance, node, "dataclass_init")


def _post_init(w: Walker, generated: ClassInfo, instance: Inst, node: ast.AST, detail: str) -> None:
    """dataclass 生成的初始化只在装饰时 MRO 上有 ``__post_init__`` 时调用它（CPython 在装饰那一刻检查），调用经
    ``self`` 按实例派发。构造、``super().__init__`` 与 ``dataclasses.replace`` 都由这里决定是否调用。"""
    if generated.generated_init and w.program.lookup(generated.cid, "__post_init__") is not None:
        post_init = w.program.lookup(instance.cls, "__post_init__")
        assert post_init is not None  # 生成初始化的类在实例的 MRO 上
        w.call_member(PROTOCOL, post_init, instance, node, detail=detail)


def _super(w: Walker, node: ast.Call) -> Values:
    argvals = _evaluate_arguments(w, node)
    if len(node.args) == 2:
        owners = [value.cls for value in argvals[id(node.args[0])] if isinstance(value, ClassObj)]
        receivers = argvals[id(node.args[1])]
    else:
        frame = w.method_frame()
        if frame is None or frame.function is None or frame.function.owner is None:
            return EMPTY
        signature = frame.function.signature
        assert signature is not None
        owners = [frame.function.owner.cid]
        receivers = frame.names.get(signature.positional[0], EMPTY) if signature.positional else EMPTY
    return frozenset(SuperV(owner, receivers) for owner in owners)


def _super_init(w: Walker, value: SuperV, node: ast.Call, argvals: ArgValues) -> None:
    """``super().__init__(...)`` 走到 dataclass 生成的初始化时，没有方法可查，按初始化链处理。"""
    for receiver in value.receivers:
        if isinstance(receiver, Inst):
            explicit, generated = w.program.init_provider(receiver.cls, value.owner)
            if explicit is None and generated is not None:
                _initialize(w, receiver.cls, receiver, node, argvals, value.owner)


class _Arguments:
    """外部 API 一次调用的实参：按它在 Python 3.8 的签名经 ``binder`` 绑定，按形参名取。
    调用处有 ``*x`` 时一个实参可能落在几个形参上，一个形参也就可能收到几个实参。"""

    def __init__(self, api: ExternalApi, node: ast.Call, argvals: ArgValues) -> None:
        self.binding = bind(api.signature, node.args, node.keywords, bound=False)
        self.vararg = api.signature.vararg
        self.argvals = argvals
        self.passed: Dict[str, List[ast.expr]] = {}
        for param, expr in self.binding.arguments:
            self.passed.setdefault(param, []).append(expr)

    def exprs(self, name: str) -> Sequence[ast.expr]:
        """形参 name 可能收到的实参表达式（``*`` 形参是落进它的全部位置实参），没传时为空。"""
        return self.binding.var_positional if name == self.vararg else self.passed.get(name, ())

    def each(self, name: str) -> List[Values]:
        """形参 name 收到的各实参的取值，每个实参一项（``*x`` 的取值是它展开出的元素）。"""
        return [self.argvals[id(expr)] for expr in self.exprs(name)]

    def values(self, name: str) -> Values:
        return join(*self.each(name))


def _external_call(w: Walker, name: str, node: ast.Call, argvals: ArgValues) -> Values:
    """登记的外部 API：按签名绑定实参，调用登记的协议链，完整迭代登记的形参，再按 API 给出返回值，
    并入它可能原样返回的实参。其余外部符号只有调用外部类才确知得到它的实例；外部函数与成员的返回值类型未知。"""
    api = EXTERNAL_API.get(name)
    if api is None:
        return external_instance(w.program.index.externals, name)
    arguments = _Arguments(api, node, argvals)
    answered: List[Values] = []
    for use in api.protocols:
        operands = [EMPTY if param is None else arguments.values(param) for param in use.operands
                    if param is None or arguments.exprs(param)]
        answered.extend(w.protocol(values, use.chain, node, operands) for values in arguments.each(use.param))
    iterated = [w.iterate(values, node) for param in api.iterates for values in arguments.each(param)]
    result = _EXTERNAL_RESULTS.get(name)
    produced = (external_instance(w.program.index.externals, name) if result is None
                else result(w, name, arguments, iterated, node))
    return join(produced, *(answered if api.protocol_result else ()), *(arguments.values(param) for param in api.returns))


def new_container(w: Walker, node: ast.AST, kind: str) -> Site:
    """在 node 处新建的空的或字面量写出的内置可变容器（kind 是 list / set / dict）：字面量、推导式，以及不带实参调用
    list / set / dict（含 dataclass 默认工厂 ``field(default_factory=list)`` 每个实例建的那个）。之后的改写记在这个位置上。"""
    assert isinstance(node, LOCATED)
    return Site(w.rel, node.lineno, node.col_offset, kind)


_EMPTY_CONSTRUCTED = {"builtins.list": "list", "builtins.set": "set"}


def _collected(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """list / tuple / set / frozenset / sorted：按已有元素构造的容器与 ``copy()`` 一样按元素序列跟踪，元素是迭代实参得到的值；
    不传实参时 list() / set() 与空字面量一样是新建的空容器（``new_container``），tuple() / frozenset() 只知道是该类的实例。
    sorted 还把元素两两比较（``_order``）；set / frozenset 插入元素时对元素取哈希。"""
    if name == "builtins.sorted":
        _order(w, arguments, join(*iterated), ast.Lt, node)
    if name in _HASHED_COLLECTIONS:
        w.protocol(join(*iterated), HASH, node)
    if iterated:
        return frozenset({SeqT(join(*iterated))})
    if name in _EMPTY_CONSTRUCTED:
        return frozenset({new_container(w, node, _EMPTY_CONSTRUCTED[name])})
    return external_instance(w.program.index.externals, name)


def _dict(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """dict(...)：不带实参时与 ``{}`` 一样是新建的空字典（``new_container``）。带实参时库按 ``mapping_items`` 读取位置实参
    与 ``**x``，插入时对键取哈希（与 map 一样只记它对实参调用的协议成员），得到的字典内容不跟踪，只知道是 dict 的实例。"""
    if not node.args and not node.keywords:
        return frozenset({new_container(w, node, "dict")})
    spread = [arguments.argvals[id(keyword.value)] for keyword in node.keywords if keyword.arg is None]
    for source in join(*arguments.each("iterable"), *spread):
        keys, _values = mapping_items(w, source, node)
        w.protocol(keys, HASH, node)
    return external_instance(w.program.index.externals, name)


def mapping_items(w: Walker, source: Value, node: ast.AST) -> Tuple[Values, Values]:
    """``dict(source)``、``d.update(source)``、``{**source}`` 取到的 (键, 值)：字典与映射取它的键值；有 ``keys`` 方法的
    仓库实例按映射协议取 ``keys()`` 与 ``source[键]``；其余迭代出 (键, 值) 对。"""
    if isinstance(source, Site) and source.kind == "dict":
        return w.solver.read(("keys", source)), w.solver.read(("values", source))
    if isinstance(source, MapT):
        return source.keys, source.values
    single = frozenset({source})
    if isinstance(source, Inst) and w.program.lookup(source.cls, "keys") is not None:
        keys = w.iterate(w.protocol(single, ("keys",), node), node)
        return keys, w.protocol(single, (SUBSCRIPT_MEMBERS[ast.Load],), node, (keys,))
    keys_found: List[Values] = []
    values_found: List[Values] = []
    for pair in w.iterate(single, node):
        if isinstance(pair, Tup) and len(pair.items) == 2:
            keys_found.append(pair.items[0])
            values_found.append(pair.items[1])
        else:
            items = w.elements(pair)
            keys_found.append(items)
            values_found.append(items)
    return join(*keys_found), join(*values_found)


def _field(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """dataclasses.field：字段取值是 default（经 ``returns`` 并入），或生成的 __init__ 每次以零实参调用
    default_factory 的结果（``default_factory=list`` 每个实例一个新列表）。"""
    return invoke(w, arguments.values("default_factory"), *library_call(node, ()))


def _tested(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """any / all：对迭代出的每个元素做真值测试，返回布尔。"""
    w.protocol(join(*iterated), TRUTH, node)
    return EMPTY


def _summed(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """sum：从 start 起逐个加上迭代出的元素（``total = total + 元素``，上一次的和是下一次的左操作数），
    返回 start 或某次加法的结果。"""
    elements = join(*iterated)
    total = arguments.values("start")
    while True:
        merged = join(total, w.binary(total, ("__add__",), elements, "__radd__", node))
        if merged == total:
            return total
        total = merged


def _enumerated(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    if iterated:
        return frozenset({SeqT(frozenset({Tup((EMPTY, join(*iterated)))}))})
    return external_instance(w.program.index.externals, name)


def _zipped(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """每个元素是由各可迭代实参的元素组成的元组；带 ``*x`` 时可迭代实参的个数不定，元组长度也不定。"""
    if any(isinstance(expr, ast.Starred) for expr in arguments.exprs("iterables")):
        return frozenset({SeqT(frozenset({SeqT(join(*iterated))}))})
    return frozenset({SeqT(frozenset({Tup(tuple(iterated))}))})


def _iter(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """``iter(x)`` 只取迭代器；``iter(callable, sentinel)`` 由库在这里反复以零实参调用 callable，元素是它的返回值。"""
    if not arguments.exprs("sentinel"):
        return w.iterator(arguments.values("object"), node)
    return frozenset({SeqT(invoke(w, arguments.values("object"), *library_call(node, ())))})


def _reversed(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    return w.iterator(arguments.values("sequence"), node, BACKWARD)


def _next(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    return w.advance(arguments.values("iterator"), node)


def _pick(operator: type, w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """min / max：恰有一个位置实参时完整迭代它，返回其中一个元素；两个以上时返回其中一个实参。
    带 ``*x`` 时位置实参的个数不定，两种形式都可能。候选两两比较（``_order``，min 用 ``<``、max 用 ``>``）。"""
    exprs = arguments.exprs("args")
    items = arguments.each("args")
    plain = sum(1 for expr in exprs if not isinstance(expr, ast.Starred))
    spread = plain < len(exprs)
    found = []
    if plain == 1 or (plain == 0 and spread):
        found.append(w.iterate(join(*items), node))
    if plain >= 2 or spread:
        found.append(join(*items))
    candidates = join(*found)
    _order(w, arguments, candidates, operator, node)
    return candidates


def _order(w: Walker, arguments: _Arguments, items: Values, operator: type, node: ast.Call) -> None:
    """排序与取极值（sorted、``list.sort``、min、max）把比较对象两两按 operator 比较：给了 key 时比较的是库以每个元素
    调用 key 的返回值，否则是元素本身。"""
    keys = invoke(w, arguments.values("key"), *library_call(node, (items,))) if arguments.exprs("key") else items
    w.compare(keys, operator, keys, node)


def _type(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """``type(x)`` 取实例的类；三实参形式造出新类。"""
    if arguments.exprs("bases"):
        return external_instance(w.program.index.externals, name)
    return frozenset(ClassObj(value.cls) for value in arguments.values("object") if isinstance(value, Inst))


def _attribute_names(arguments: _Arguments) -> List[str]:
    """getattr / setattr 的属性名：常量字符串实参。"""
    return [key for key in map(constant_key, arguments.exprs("name")) if key is not None]


def _getattr(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    receivers = arguments.values("object")
    return join(*(w.attribute_load(receivers, attribute, node, callee=False) for attribute in _attribute_names(arguments)))


def _setattr(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    if arguments.exprs("value"):
        for attribute in _attribute_names(arguments):
            w.attribute_store(arguments.values("object"), attribute, arguments.values("value"), node)
    return EMPTY


def _cast(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    return join(*(annotation_values(w.program, w.frame.scope, w.module, expr) for expr in arguments.exprs("typ")))


def _replace(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    instances = frozenset(value for value in arguments.values("obj") if isinstance(value, Inst))
    for instance in instances:
        _initialize_by_keywords(w, instance, node)
    return instances


def _pytest_main(w: Walker, name: str, arguments: _Arguments, iterated: List[Values], node: ast.Call) -> Values:
    """插件实例上定义的 pytest 钩子由 pytest 调用。"""
    for plugin in join(*iterated):
        if isinstance(plugin, Inst):
            for hook in sorted(pytest_hook_names()):
                function = w.program.lookup(plugin.cls, hook)
                if function is not None:
                    w.call_member(PROTOCOL, function, plugin, node, detail="pytest.main")
    return external_instance(w.program.index.externals, name)


ExternalResult = Callable[["Walker", str, _Arguments, List[Values], ast.Call], Values]
# 外部 API 的返回值与额外效果；不在表里的登记 API 只知道调用外部类得到它的实例。
_EXTERNAL_RESULTS: Dict[str, ExternalResult] = {
    "builtins.list": _collected,
    "builtins.tuple": _collected,
    "builtins.set": _collected,
    "builtins.frozenset": _collected,
    "builtins.dict": _dict,
    "builtins.sorted": _collected,
    "builtins.sum": _summed,
    "builtins.any": _tested,
    "builtins.all": _tested,
    "builtins.enumerate": _enumerated,
    "builtins.zip": _zipped,
    "builtins.iter": _iter,
    "builtins.reversed": _reversed,
    "builtins.next": _next,
    "builtins.min": functools.partial(_pick, ast.Lt),
    "builtins.max": functools.partial(_pick, ast.Gt),
    "builtins.type": _type,
    "builtins.getattr": _getattr,
    "builtins.setattr": _setattr,
    "typing.cast": _cast,
    "dataclasses.field": _field,
    "dataclasses.replace": _replace,
    "pytest.main": _pytest_main,
}


def _initialize_by_keywords(w: Walker, instance: Inst, node: ast.Call) -> None:
    """``dataclasses.replace`` 用字段关键字重新构造同类实例；实参来自原实例，不另行绑定。"""
    explicit, generated = w.program.init_provider(instance.cls)
    if explicit is not None:
        w.call_member(PROTOCOL, explicit, instance, node, detail="dataclasses.replace")
    elif generated is not None:
        _post_init(w, generated, instance, node, "dataclasses.replace")


def container_method(w: Walker, value: Value, name: str, node: ast.Call, argvals: ArgValues) -> Values:
    """新建容器（``new_container``）的改写把元素写回该位置；注解给出的结构容器只读。``*x`` 实参的取值是它展开出的元素。
    集合与字典按键查找时对键取哈希（``hash_keys``）；列表与序列值的 ``sort`` 与 sorted 同一规则比较元素（``_order``）。"""
    args = [argvals[id(arg)] for arg in node.args]
    if name in _KEY_LOOKUPS and args:
        hash_keys(w, (value,), args[0], node)
    if name == "sort" and (isinstance(value, SeqT) or (isinstance(value, Site) and value.kind == "list")):
        _order(w, _Arguments(LIST_SORT, node, argvals), w.elements(value), ast.Lt, node)
    if isinstance(value, Site) and value.kind != "dict":
        elems = ("elems", value)
        if name in _LIST_ADD and args:
            add_elements(w, value, args[-1], node)
        elif name in _LIST_EXTEND and args:
            add_elements(w, value, w.iterate(args[0], node), node)
        elif name == "pop":
            return w.solver.read(elems)
        elif name == "copy":
            return frozenset({SeqT(w.solver.read(elems))})
        return EMPTY
    if isinstance(value, Site):
        key_node = node.args[0] if node.args else None
        if name == "setdefault" and args:
            dict_write(w, value, key_node, args[0], args[1] if len(args) > 1 else EMPTY, node)
        elif name == "update" and args:
            for source in args[0]:
                dict_write(w, value, None, *mapping_items(w, source, node), node)
        if name in _MAPPING_LOOKUPS:
            found = dict_read(w, value, constant_key(key_node))
            return join(found, args[1]) if len(args) > 1 else found
        return _mapping_result(name, w.solver.read(("keys", value)), w.solver.read(("values", value)))
    if isinstance(value, MapT):
        if name in _MAPPING_LOOKUPS:
            return join(value.values, args[1]) if len(args) > 1 else value.values
        return _mapping_result(name, value.keys, value.values)
    if isinstance(value, SeqT):
        return value.elems if name == "pop" else frozenset({value}) if name == "copy" else EMPTY
    return EMPTY


def constant_key(node: Optional[ast.AST]) -> Optional[str]:
    """字典的常量字符串键；别的键表达式给 None。"""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def dict_read(w: Walker, site: Site, key: Optional[str]) -> Values:
    """新建的字典按常量字符串键分桶：常量键读本桶与“非常量键”桶，其余（key 为 None）读全部值。"""
    if key is None:
        return w.solver.read(("values", site))
    return join(w.solver.read(("value", site, key)), w.solver.read(("value", site, None)))


def dict_write(w: Walker, site: Site, key_node: Optional[ast.AST], keys: Values, values: Values, node: ast.AST) -> None:
    """把键值写进新建的字典（按常量字符串键分桶，见 ``dict_read``），插入时对键取哈希。从别的字典复制时 CPython
    复用已存的哈希，这里不区分来源，多记不漏记。"""
    w.solver.write(("value", site, constant_key(key_node)), values)
    w.solver.write(("values", site), values)
    w.solver.write(("keys", site), keys)
    w.protocol(keys, HASH, node)


def add_elements(w: Walker, site: Site, values: Values, node: ast.AST) -> None:
    """把元素写进新建的列表或集合；集合插入时对元素取哈希（来源不区分，同 ``dict_write``）。"""
    w.solver.write(("elems", site), values)
    if site.kind == "set":
        w.protocol(values, HASH, node)


def hash_keys(w: Walker, containers: Iterable[Value], keys: Values, node: ast.AST) -> None:
    """按键查找或改写集合与字典（新建的 set / dict 与字典注解值）时，解释器对键调用 ``__hash__``：``in``、下标读写与删除、
    get / pop / setdefault / discard / remove。"""
    if any(isinstance(value, MapT) or (isinstance(value, Site) and value.kind != "list") for value in containers):
        w.protocol(keys, HASH, node)


def _mapping_result(name: str, keys: Values, values: Values) -> Values:
    if name == "items":
        return frozenset({SeqT(frozenset({Tup((keys, values))}))})
    if name == "values":
        return frozenset({SeqT(values)})
    if name == "keys":
        return frozenset({SeqT(keys)})
    if name == "copy":
        return frozenset({MapT(keys, values)})
    return EMPTY


def external_slot_store(w: Walker, receivers: Values, name: str, values: Values, node: ast.AST) -> None:
    """赋给外部类实例确切属性的值，由库按 ``EXTERNAL_ATTRIBUTE_SLOTS`` 登记的方式调用并消费调用结果。

    接收者的外部类型身份按真实继承链判断：外部类的实例取该类的继承链，仓库类的实例取它的外部基类的继承链。
    库的调用按真实形状合成在赋值处：位置实参是库传入的值（类型未知），记为一次已解析调用。
    """
    for (owner, attribute), slot in EXTERNAL_ATTRIBUTE_SLOTS.items():
        if attribute == name and any(owner in _external_ancestry(w, receiver) for receiver in receivers):
            call, argvals = library_call(node, [EMPTY for _argument in slot.arguments])
            response = invoke(w, values, call, argvals)
            w.iterate(response, call)
            for chain in slot.finish:
                w.protocol(response, chain, call)


def _external_ancestry(w: Walker, receiver: Value) -> FrozenSet[str]:
    if isinstance(receiver, ExternalInst):
        return frozenset(w.program.index.externals.mro(receiver.symbol))
    if isinstance(receiver, Inst):
        return w.program.external_ancestry(receiver.cls)
    return frozenset()


def subscript_store(w: Walker, receivers: Values, target: ast.Subscript, values: Values) -> None:
    index = subscript_index(target)
    key = w.ev(index)
    w.protocol(receivers, (SUBSCRIPT_MEMBERS[ast.Store],), target, (key, values))
    # 字典注解值的内容不跟踪，按键写入只取哈希；新建的字典在 dict_write 里取。
    hash_keys(w, (receiver for receiver in receivers if isinstance(receiver, MapT)), key, target)
    for receiver in receivers:
        if isinstance(receiver, Site) and receiver.kind == "dict":
            dict_write(w, receiver, index, key, values, target)
        elif isinstance(receiver, Site):
            add_elements(w, receiver, values, target)


def write_defaults(w: Walker, function: FunctionInfo) -> None:
    """默认值在定义处求值，并作为对应形参的一种取值。"""
    signature = function.signature
    assert signature is not None
    for name, default in signature.defaults.items():
        w.solver.write(param_key(function.qual, name), w.ev(default))
