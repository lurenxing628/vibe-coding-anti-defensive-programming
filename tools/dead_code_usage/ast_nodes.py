"""AST 使用图的内部节点工具。"""
from __future__ import annotations

import ast
from collections import defaultdict
from typing import Dict, Optional, Set, Tuple

from .model import FunctionRecord


class FunctionNode:
    def __init__(self, record, node):
        # type: (FunctionRecord, ast.AST) -> None
        self.record = record
        self.node = node
        self.params = param_names(node)


class ModuleInfo:
    def __init__(self, rel, module, tree):
        # type: (str, str, ast.Module) -> None
        self.rel = rel
        self.module = module
        self.tree = tree
        self.module_aliases = {}  # type: Dict[str, str]
        self.direct_aliases = {}  # type: Dict[str, str]
        self.class_aliases = {}  # type: Dict[str, str]
        self.non_class_names = set()  # type: Set[str]


def functions_by_rel(functions):
    grouped = defaultdict(list)
    for item in functions.values():
        grouped[item.record.rel].append(item)
    for rel in grouped:
        grouped[rel].sort(key=lambda item: (item.record.line, item.record.end))
    return grouped


def function_node_lookup(tree):
    # type: (ast.Module) -> Dict[Tuple[int, str, Optional[str]], ast.AST]
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[(line(node), node.name, None)] = node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    result[(line(sub), sub.name, node.name)] = sub
    return result


def class_id(rel, cls):
    # type: (str, str) -> str
    return f"{rel}::{cls}"


def param_names(node):
    args = getattr(node, "args", None)
    if args is None:
        return []
    params = []
    for arg in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        params.append(arg.arg)
    if getattr(args, "vararg", None):
        params.append(args.vararg.arg)
    if getattr(args, "kwarg", None):
        params.append(args.kwarg.arg)
    return params


def line(node):
    return int(getattr(node, "lineno", 0) or 0)


def position(node):
    return (line(node), int(getattr(node, "col_offset", 0) or 0))


def end_position(node):
    start_line, start_col = position(node)
    return (
        int(getattr(node, "end_lineno", start_line) or start_line),
        int(getattr(node, "end_col_offset", start_col) or start_col),
    )


def before_position(node, before):
    if before is None:
        return True
    if not isinstance(before, tuple):
        before = (int(before), 0)
    return end_position(node) <= before
