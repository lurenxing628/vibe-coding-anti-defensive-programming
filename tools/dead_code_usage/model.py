"""死代码使用图的数据结构。"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from .binder import Binding

# 函数引用的种类。
CALL = "call"  # 已解析的调用（含库调用赋给外部属性的值，合成在赋值处），附实参绑定
CONSTRUCTOR = "constructor"  # 构造类实例时调用到的 __init__ / __new__
REFERENCE = "reference"  # 非调用引用：函数或绑定方法作为值被取走（逃逸）
PROPERTY = "property"  # 属性读取或赋值命中 property 的 getter / setter
PROTOCOL = "protocol"  # 运算、内置函数或外部 API 隐式调用的协议成员
DECORATOR = "decorator"  # 函数交给会改写它的装饰器（外部注册、仓库装饰器）
CALLBACK = "callback"  # 外部基类会回调的确切方法
# 模块级变量引用的种类。
LOAD = "load"
STORE = "store"
DELETE = "delete"


@dataclass(frozen=True)
class CodeRoles:
    """证据来源用的代码角色，由调用方给出（扫描器传 ``tools/path_roles.py`` 的 ``role_of`` 与 ``TEST``），引擎自己不划分：
    of(rel) 是文件的角色；test 是测试代码的角色，测试文件第二段才加入求解（``build_reference_index``）。"""

    of: Callable[[str], str]
    test: str


@dataclass(frozen=True)
class FunctionRecord:
    qual: str
    rel: str
    line: int
    end: int
    cls: Optional[str]
    name: str


@dataclass(frozen=True)
class UsageEvidence:
    """一条使用证据；origin 的含义同 ``Reference.origin``。"""

    target: str
    source_rel: str
    line: int
    kind: str
    detail: str
    origin: str


@dataclass(frozen=True)
class Reference:
    """一处引用。target 是函数限定名，或模块级变量 ``rel::NAME``。

    line/col 到 end_line/end_col 是记下引用的那个节点的完整跨度（``span``），起点相同的嵌套节点由它区分；
    source_qual 是引用所在函数的限定名，模块与类体顶层记 ``<module>``；binding 只在调用类引用上给出；
    context 只在模块级变量引用上给出：从直接父节点到所在语句的祖先链，供消费方判断读写与逃逸。
    origin 是给出这条证据的代码角色（``CodeRoles``）：只用生产与工具代码求解就有的引用取所在文件的角色；
    加入测试代码后才出现的引用记测试角色，包括测试文件里的引用，以及测试传入的类型经生产代码形成的引用。
    """

    target: str
    source_rel: str
    line: int
    col: int
    end_line: int
    end_col: int
    source_qual: str
    kind: str
    detail: str
    origin: str
    binding: Optional[Binding] = None
    context: Tuple[ast.AST, ...] = ()

    @property
    def span(self) -> Tuple[int, int, int, int]:
        return self.line, self.col, self.end_line, self.end_col


@dataclass(frozen=True)
class AttributeSite:
    """接收者类型没能解析的属性访问（含 ``getattr`` 常量名）；origin 的含义同 ``Reference.origin``。"""

    name: str
    source_rel: str
    line: int
    col: int
    source_qual: str
    origin: str


def module_name_from_rel(rel: str) -> str:
    path = rel[:-3] if rel.endswith(".py") else rel
    parts = path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(part for part in parts if part)


def records_from_functions(functions: Dict[str, Dict[str, Any]]) -> Dict[str, FunctionRecord]:
    return {
        qual: FunctionRecord(
            qual=qual,
            rel=info["rel"],
            line=info["line"],
            end=info["end"],
            cls=info["cls"],
            name=info["name"],
        )
        for qual, info in functions.items()
    }


def rel_join(root: str, rel: str) -> str:
    return os.path.join(root, rel.replace("/", os.sep))
