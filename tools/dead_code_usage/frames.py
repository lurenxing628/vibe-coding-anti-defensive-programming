"""遍历时的名字帧：单元自身一层，内联遍历的 lambda、推导式、类体各一层。"""
from __future__ import annotations

from typing import Dict, Optional, Set

from .program import ClassInfo, FunctionInfo
from .scope import Scope
from .values import Values


class Frame:
    __slots__ = ("scope", "names", "source_qual", "cls", "function")

    def __init__(self, scope: Scope, source_qual: str, cls: Optional[ClassInfo] = None,
                 function: Optional[FunctionInfo] = None) -> None:
        self.scope = scope
        self.names: Dict[str, Values] = {}
        self.source_qual = source_qual
        self.cls = cls
        self.function = function


def param_names(function: FunctionInfo) -> Set[str]:
    args = function.args
    names = {arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    for arg in (args.vararg, args.kwarg):
        if arg is not None:
            names.add(arg.arg)
    return names
