"""隐式调用关系表：运算与外部 API 会调用操作数的哪些协议成员，外部基类会回调子类的哪些确切方法，
外部对象的哪些确切属性里存放的值会被库调用。

所有条目都按“谁在调用”（运算种类、外部函数全名、外部基类全名、外部类与属性名）登记，不按被调方法的名字豁免：
只有操作数的类型已经解析到仓库类、或类型的真实继承链上确有该外部类时，对应成员才得到证据。
每个协议链按顺序取 MRO 里第一个有定义的成员（如真值测试先找 ``__bool__``、没有再找 ``__len__``）。
迭代（取迭代器、推进、完整迭代）由 ``Walker`` 统一建模，外部 API 只登记它完整迭代哪个形参；消费型 API 对元素做的
运算（真值测试、加法、比较）与对应表达式走同一套派发（见 ``calls._EXTERNAL_RESULTS``）。
外部类的继承链与 pytest 钩子名都从已安装的库本身读取；外部符号的读取与身份见 ``ExternalSymbols``。
"""
from __future__ import annotations

import ast
import functools
import importlib
import importlib.util
import inspect
import os
import sys
import sysconfig
import typing
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from typing import Callable, Dict, FrozenSet, Iterable, Optional, Tuple

from .binder import Signature, signature_of

Chain = Tuple[str, ...]

TRUTH: Chain = ("__bool__", "__len__")
TO_STR: Chain = ("__str__", "__repr__")
FORMAT: Chain = ("__format__", "__str__", "__repr__")
REPR: Chain = ("__repr__",)
HASH: Chain = ("__hash__",)
LENGTH: Chain = ("__len__",)
INT: Chain = ("__int__", "__index__")
FLOAT: Chain = ("__float__", "__index__")
INDEX: Chain = ("__index__",)
# 内置容器自己实现的协议成员对元素再做的协议调用（``Walker.protocol`` 统一展开）：文本转换（str、repr、format，含 print、
# f-string 与 ``%`` 格式化）对每个元素取 repr，字典对键与值都取；元组与 frozenset 的哈希对每个元素取哈希。
CONTAINER_PROTOCOLS: Dict[Chain, Chain] = {TO_STR: REPR, REPR: REPR, FORMAT: REPR, HASH: HASH}

# 取迭代器：(方法, 没有该方法时走旧式序列协议前先调用的成员)。实例有该方法就调用它，返回值是迭代器；
# 没有时得到按下标取元素的迭代器（``__getitem__``），倒序还要先取长度。
IteratorProtocol = Tuple[str, Chain]
FORWARD: IteratorProtocol = ("__iter__", ())
BACKWARD: IteratorProtocol = ("__reversed__", LENGTH)

BINARY_OPERATORS: Dict[type, Tuple[str, str, str]] = {
    ast.Add: ("__add__", "__radd__", "__iadd__"),
    ast.Sub: ("__sub__", "__rsub__", "__isub__"),
    ast.Mult: ("__mul__", "__rmul__", "__imul__"),
    ast.MatMult: ("__matmul__", "__rmatmul__", "__imatmul__"),
    ast.Div: ("__truediv__", "__rtruediv__", "__itruediv__"),
    ast.FloorDiv: ("__floordiv__", "__rfloordiv__", "__ifloordiv__"),
    ast.Mod: ("__mod__", "__rmod__", "__imod__"),
    ast.Pow: ("__pow__", "__rpow__", "__ipow__"),
    ast.LShift: ("__lshift__", "__rlshift__", "__ilshift__"),
    ast.RShift: ("__rshift__", "__rrshift__", "__irshift__"),
    ast.BitOr: ("__or__", "__ror__", "__ior__"),
    ast.BitXor: ("__xor__", "__rxor__", "__ixor__"),
    ast.BitAnd: ("__and__", "__rand__", "__iand__"),
}
# 比较：(左操作数的链, 右操作数的反射链)。
COMPARE_OPERATORS: Dict[type, Tuple[Chain, Chain]] = {
    ast.Eq: (("__eq__",), ("__eq__",)),
    ast.NotEq: (("__ne__", "__eq__"), ("__ne__", "__eq__")),
    ast.Lt: (("__lt__",), ("__gt__",)),
    ast.LtE: (("__le__",), ("__ge__",)),
    ast.Gt: (("__gt__",), ("__lt__",)),
    ast.GtE: (("__ge__",), ("__le__",)),
}
UNARY_OPERATORS: Dict[type, Chain] = {
    ast.USub: ("__neg__",),
    ast.UAdd: ("__pos__",),
    ast.Invert: ("__invert__",),
    ast.Not: TRUTH,
}
SUBSCRIPT_MEMBERS: Dict[type, str] = {ast.Load: "__getitem__", ast.Store: "__setitem__", ast.Del: "__delitem__"}
# 字符串 ``%`` 格式化的转换符 → 对操作数调用的协议链（Python 3.8 实测）；``%%`` 不取操作数。
PERCENT_CONVERSIONS: Dict[str, Chain] = {
    "s": TO_STR, "r": REPR, "a": REPR,
    **dict.fromkeys("diu", INT), **dict.fromkeys("oxXc", INDEX), **dict.fromkeys("eEfFgG", FLOAT),
}


@dataclass(frozen=True)
class ProtocolCall:
    """外部 API 对形参 param 的实参按 chain 调用的协议成员；operands 是它按位置转交给该成员的实参：形参名取调用处
    传入的值（没传就不转交），None 是库自己算出的整数（如插入位置）。"""

    param: str
    chain: Chain
    operands: Tuple[Optional[str], ...] = ()


@dataclass(frozen=True)
class ExternalApi:
    """外部函数：signature 是它在 Python 3.8 的签名，调用处的实参经 ``binder`` 按它绑定，关键字实参按名对上形参；
    protocols 是它调用的协议成员；iterates 是它完整迭代（取迭代器再推进到底）的形参；
    returns 是它可能把实参原样返回的形参（取不到时给 default，``cast`` 给回 val）；
    protocol_result 表示它原样返回所调协议成员的返回值（``abs(x)`` 就是 ``x.__abs__()``）。"""

    signature: Signature
    protocols: Tuple[ProtocolCall, ...]
    iterates: Tuple[str, ...]
    returns: Tuple[str, ...]
    protocol_result: bool


def _api(params: str, protocols: Iterable[ProtocolCall] = (), iterates: Iterable[str] = (),
         returns: Iterable[str] = (), protocol_result: bool = False) -> ExternalApi:
    """params 是 ``def`` 写法的形参表（含 ``/`` 与默认值）。"""
    function = ast.parse(f"def api({params}): pass").body[0]
    assert isinstance(function, ast.FunctionDef)
    return ExternalApi(signature_of(function.args, exact=True), tuple(protocols), tuple(iterates), tuple(returns), protocol_result)


_SEQUENCE = _api("iterable=(), /", iterates=["iterable"])
_INSORT = _api("a, x, lo=0, hi=None", [
    ProtocolCall("a", ("insert",), (None, "x")), ProtocolCall("a", LENGTH), ProtocolCall("a", ("__getitem__",), (None,)),
])
_BISECT = _api("a, x, lo=0, hi=None", [ProtocolCall("a", LENGTH), ProtocolCall("a", ("__getitem__",), (None,))])
_PICK = _api("*args, key=None, default=None", returns=["default"])
# 列表的 ``sort`` 方法：新建列表与序列值调用它时按这个签名绑定实参（``calls.container_method``）。
LIST_SORT = _api("*, key=None, reverse=False")

# 外部函数全名 → 它的签名与它对实参做的事；返回值的形状、迭代方式与对元素做的运算（any/all 的真值测试、sum 的
# 加法、min/max/sorted 的比较）见 ``calls._EXTERNAL_RESULTS``。
EXTERNAL_API: Dict[str, ExternalApi] = {
    "builtins.len": _api("obj, /", [ProtocolCall("obj", LENGTH)]),
    "builtins.bool": _api("x=False, /", [ProtocolCall("x", TRUTH)]),
    "builtins.str": _api("object='', encoding='utf-8', errors='strict'", [ProtocolCall("object", TO_STR)], protocol_result=True),
    "builtins.repr": _api("obj, /", [ProtocolCall("obj", REPR)], protocol_result=True),
    "builtins.format": _api("value, format_spec='', /", [ProtocolCall("value", FORMAT, ("format_spec",))], protocol_result=True),
    "builtins.print": _api("*objects, sep=' ', end='', file=None, flush=False", [ProtocolCall("objects", TO_STR)]),
    "builtins.hash": _api("obj, /", [ProtocolCall("obj", HASH)]),
    "builtins.abs": _api("x, /", [ProtocolCall("x", ("__abs__",))], protocol_result=True),
    "builtins.int": _api("x=0, /, base=10", [ProtocolCall("x", INT)]),
    "builtins.float": _api("x=0.0, /", [ProtocolCall("x", FLOAT)]),
    "builtins.round": _api("number, ndigits=None", [ProtocolCall("number", ("__round__",), ("ndigits",))], protocol_result=True),
    "builtins.next": _api("iterator, default=None, /", returns=["default"]),
    "builtins.iter": _api("object, sentinel=None, /"),
    "builtins.reversed": _api("sequence, /"),
    "builtins.list": _SEQUENCE,
    "builtins.tuple": _SEQUENCE,
    "builtins.set": _SEQUENCE,
    "builtins.frozenset": _SEQUENCE,
    "builtins.dict": _api("iterable=(), /, **kwargs"),
    "builtins.sorted": _api("iterable, /, *, key=None, reverse=False", iterates=["iterable"]),
    "builtins.sum": _api("iterable, /, start=0", iterates=["iterable"]),
    "builtins.any": _api("iterable, /", iterates=["iterable"]),
    "builtins.all": _api("iterable, /", iterates=["iterable"]),
    "builtins.min": _PICK,
    "builtins.max": _PICK,
    "builtins.enumerate": _api("iterable, start=0", iterates=["iterable"]),
    "builtins.zip": _api("*iterables", iterates=["iterables"]),
    "builtins.map": _api("func, /, *iterables", iterates=["iterables"]),
    "builtins.filter": _api("function, iterable, /", iterates=["iterable"]),
    "builtins.type": _api("object, bases=None, dict=None, /"),
    "builtins.getattr": _api("object, name, default=None, /", returns=["default"]),
    "builtins.setattr": _api("object, name, value, /"),
    "bisect.insort": _INSORT,
    "bisect.insort_left": _INSORT,
    "bisect.insort_right": _INSORT,
    "bisect.bisect": _BISECT,
    "bisect.bisect_left": _BISECT,
    "bisect.bisect_right": _BISECT,
    "copy.copy": _api("x", [ProtocolCall("x", ("__copy__",))], protocol_result=True),
    "copy.deepcopy": _api("x, memo=None, _nil=[]", [ProtocolCall("x", ("__deepcopy__",), ("memo",))], protocol_result=True),
    "typing.cast": _api("typ, val", returns=["val"]),
    "dataclasses.field": _api(
        "*, default=MISSING, default_factory=MISSING, init=True, repr=True, hash=None, compare=True, metadata=None",
        returns=["default"],
    ),
    "dataclasses.replace": _api("obj, /, **changes"),
    "pytest.main": _api("args=None, plugins=None", iterates=["plugins"]),
}

# 不改变函数被调用方式的装饰器：被装饰的函数仍按名字调用，装饰本身不算使用。
TRANSPARENT_DECORATORS = frozenset({
    "builtins.staticmethod",
    "builtins.classmethod",
    "builtins.property",
    "functools.cached_property",
    "functools.lru_cache",
    "functools.cache",
    "functools.wraps",
    "abc.abstractmethod",
    "contextlib.contextmanager",
    "typing.overload",
    "typing.final",
})

# 外部基类会回调的确切方法：子类覆写后由库本身调用。
EXTERNAL_CALLBACKS: Dict[str, FrozenSet[str]] = {
    "html.parser.HTMLParser": frozenset({
        "handle_starttag", "handle_endtag", "handle_startendtag", "handle_data", "handle_entityref",
        "handle_charref", "handle_comment", "handle_decl", "handle_pi", "unknown_decl",
    }),
    "socketserver.BaseServer": frozenset({
        "server_close", "get_request", "verify_request", "process_request", "finish_request", "shutdown_request",
        "close_request", "handle_error", "handle_timeout", "service_actions",
    }),
    # 绑定与监听由 TCPServer.__init__ 调用，直接继承 BaseServer 的类不会走到。
    "socketserver.TCPServer": frozenset({"server_bind", "server_activate"}),
    "flask.json.provider.JSONProvider": frozenset({"dumps", "loads", "response"}),
    "builtins.BaseException": frozenset({"__str__"}),
}


@dataclass(frozen=True)
class ResponderSlot:
    """外部对象上存放“应用”的属性：库以 arguments 为位置实参调用存进去的值，把调用结果当作响应迭代，
    结束后对它取 finish 里的各协议链。"""

    arguments: Tuple[str, ...]
    finish: Tuple[Chain, ...]


# WSGI 应用（PEP 3333）：以 (environ, start_response) 调用；服务器迭代返回值，结束后调用它的 close。
WSGI_APPLICATION = ResponderSlot(("environ", "start_response"), (("close",),))

# (声明该属性的外部类全名, 属性名) → 库怎样使用赋给该属性的值。接收者的真实继承链上有这个外部类
# （外部类的实例，或继承它的仓库类的实例）才算；赋给别的对象的同名属性不算。
EXTERNAL_ATTRIBUTE_SLOTS: Dict[Tuple[str, str], ResponderSlot] = {
    # Flask.__call__ 把每个请求交给实例的 wsgi_app。
    ("flask.app.Flask", "wsgi_app"): WSGI_APPLICATION,
}


# 解释器的库目录（标准库与 site-packages）。本机的 .venv 在仓库根下，装在那里的库不因此算仓库模块。
_LIBRARY_PATHS = ("stdlib", "platstdlib", "purelib", "platlib")


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _inside(path: str, directory: str) -> bool:
    return path == directory or path.startswith(os.path.join(directory, ""))


class ExternalSymbols:
    """一次扫描里仓库外的符号：导入目标、属性与内置名的统一身份，以及外部类的继承链。

    名字按 Python 的导入语义逐段读取（``_read_path``），每读一个模块先判它属不属于被扫描仓库：首段是仓库已索引的
    模块或包，或者模块的来源（文件、包目录）落在仓库目录里而不在解释器的库目录里，都是仓库模块，不读也不导入，
    没有外部符号。原始导入目标、属性，以及由模块与类的声明全名派生的名字都只经这里读取，模块只在这里导入。
    分类取决于被扫描的仓库，读取结果按扫描各缓存一份。
    """

    def __init__(self, repo_root: str, indexed: Callable[[str], bool]) -> None:
        """indexed(name) 判断 name 是不是被扫描仓库已索引的模块或包（``imports.ModuleIndex.is_module``）。"""
        self._repo_root = _real(repo_root)
        self._library_dirs = tuple(_real(sysconfig.get_path(key)) for key in _LIBRARY_PATHS)
        self._indexed = indexed
        self._read = functools.lru_cache(maxsize=None)(self._read_path)
        self._resolve = functools.lru_cache(maxsize=None)(self._resolve_path)
        self.mro = functools.lru_cache(maxsize=None)(self._class_mro)

    def symbol(self, dotted: str) -> Optional[str]:
        """仓库外的导入目标 dotted（``import a.b``、``from a import b`` 的 ``a.b``）读到的外部符号：首段是顶层模块，
        之后逐段取属性（``attribute``），途经叶子即结束；属于仓库或途经叶子时没有符号（None）。"""
        head, *rest = dotted.split(".")
        resolved = self._resolve(head)
        if resolved is None:
            return None
        symbol = resolved[0]
        for name in rest:
            attribute = self.attribute(symbol, name)
            if attribute is None:
                return None
            symbol = attribute
        return symbol

    def attribute(self, symbol: str, name: str) -> Optional[str]:
        """外部符号 symbol 的属性 name：命名空间的属性仍是外部符号；叶子的属性是值，不再是符号（None，访问记为
        未解析）；属于仓库的也没有符号。属性链只在命名空间之间延伸，而每个真实模块与类只有一个符号，经别名成环的
        属性链（``root = root.path.os``）也只得到有限个符号。"""
        if self._resolve(symbol) != (symbol, True):
            return None
        resolved = self._resolve(f"{symbol}.{name}")
        return None if resolved is None else resolved[0]

    def builtin(self, name: str) -> str:
        """内置名 name 的外部符号。"""
        resolved = self._resolve(f"builtins.{name}")
        assert resolved is not None  # builtins 是解释器内置模块，不属于仓库
        return resolved[0]

    def _resolve_path(self, dotted: str) -> Optional[Tuple[str, bool]]:
        """dotted 读到的外部符号：(统一身份, 是否命名空间)；属于仓库时没有符号（None）。外部符号的身份只由这里给出。

        模块与类是命名空间，身份是它自己声明的全名：模块取 ``__name__``，类取 ``__module__.__qualname__``（与
        ``mro`` 给出的类名同一口径）。同一个真实模块或类因此只有一个符号，经别的模块转出（``flask.Flask`` 就是
        ``flask.app.Flask``）、经别名环回到（``os.path.os`` 就是 ``os``）都是它。声明全名要能经同一套读取读回它自己；
        读不回的（``types.FunctionType`` 声明为 ``builtins.function``，或声明在仓库模块里）与别的对象（函数、实例）、
        读不到的名字都是叶子，身份就是读到它的名字 dotted：命名空间的身份加属性名，或读不到的顶层模块名。
        """
        in_repository, target = self._read(dotted)
        if in_repository:
            return None
        if inspect.ismodule(target) or inspect.isclass(target):
            declared = target.__name__ if inspect.ismodule(target) else f"{target.__module__}.{target.__qualname__}"
            if self._read(declared)[1] is target:
                return declared, True
        return dotted, False

    def _read_path(self, dotted: str) -> Tuple[bool, object]:
        """按 Python 的导入语义逐段读取 dotted：(属于仓库, 读到的对象)，读不到时对象是 None。

        首段是顶层模块，已索引的属于仓库；之后每段在模块上先取属性（含模块级 ``__getattr__`` 给出的惰性属性，如
        ``concurrent.futures.ThreadPoolExecutor``），没有再取同名子模块；在类上取类属性；途经模块与类以外的对象时
        读不到。每个模块都先经 ``_module`` 判定归属再导入，查找子模块不会先执行仓库的父包。读不到的还有：模块在本
        解释器里找不到（只在 Windows 上有的 ``winreg``/``msvcrt``、开发环境没装的 ``PyInstaller``/``jedi``），
        或属性只在别的平台存在（``ctypes.WinDLL``）。
        """
        parts = dotted.split(".")
        if self._indexed(parts[0]):
            return True, None
        target: object = None
        for index, part in enumerate(parts):
            if index and not (inspect.ismodule(target) or inspect.isclass(target)):
                return False, None
            if index and hasattr(target, part):
                target = getattr(target, part)
            elif not index or hasattr(target, "__path__"):
                in_repository, target = self._module(".".join(parts[: index + 1]))
                if in_repository:
                    return True, None
            else:
                return False, None
        return False, target

    def _module(self, name: str) -> Tuple[bool, object]:
        """名为 name 的模块：(属于仓库, 模块)。已加载的直接用 ``sys.modules`` 里的对象（含没有 ``__spec__`` 的动态模块，
        它没有来源，不属于仓库）；没加载的先用 ``find_spec`` 找到来源，属于仓库的不导入；找不到时模块是 None。"""
        loaded = sys.modules.get(name)
        spec = importlib.util.find_spec(name) if loaded is None else getattr(loaded, "__spec__", None)
        if self._in_repository(spec):
            return True, None
        if loaded is not None:
            return False, loaded
        return False, None if spec is None else importlib.import_module(name)

    def _in_repository(self, spec: Optional[ModuleSpec]) -> bool:
        """模块的来源（文件或包目录）有一处落在被扫描仓库目录里、又不在解释器的库目录里，就是仓库模块（如测试经
        ``sys.path`` 加入的仓库内脚本目录）。本机的 ``.venv`` 在仓库根下，装在那里的库靠库目录排除；库目录在仓库外时
        这条排除用不上，判定照样成立。"""
        if spec is None:
            return False
        paths = list(spec.submodule_search_locations or ())
        if spec.has_location:
            assert spec.origin is not None  # 有位置的 spec 以 origin 记文件路径
            paths.append(spec.origin)
        return any(self._repository_path(_real(path)) for path in paths)

    def _repository_path(self, path: str) -> bool:
        return _inside(path, self._repo_root) and not any(_inside(path, directory) for directory in self._library_dirs)

    def _class_mro(self, symbol: str) -> Tuple[str, ...]:
        """外部符号的真实继承链（含自身），每项是 ``模块.类名``；符号不代表类时为空。

        typing 的泛型别名（``Dict``、``MutableSequence`` 等）取它代表的类。终点不是类（函数、``Any``、
        ``datetime.datetime.now`` 这类类属性）或读不到时，调用它得到的值类型未知。
        """
        target = self._read(symbol)[1]
        origin = typing.get_origin(target)
        klass = target if origin is None else origin
        if not inspect.isclass(klass):
            return ()
        return tuple(f"{base.__module__}.{base.__qualname__}" for base in inspect.getmro(klass))


@functools.lru_cache(maxsize=None)
def pytest_hook_names() -> FrozenSet[str]:
    """pytest 插件钩子的全部规格名，从 pytest 自己的钩子规格模块读取。"""
    hookspec = importlib.import_module("_pytest.hookspec")
    return frozenset(name for name in vars(hookspec) if name.startswith("pytest_"))
