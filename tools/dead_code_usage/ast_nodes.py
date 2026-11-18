"""AST 使用图的节点小工具：Python 3.8 下标包装、节点跨度、常量下标、lambda 记录名。"""
from __future__ import annotations

import ast
from typing import Optional, Tuple

FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
LOCATED = (ast.expr, ast.stmt, ast.excepthandler, ast.arg)


def subscript_index(node: ast.Subscript) -> ast.AST:
    """下标表达式本体：Python 3.8 把它包在 ``ast.Index`` 里，3.9 起直接是表达式。

    typeshed 已不为 ``ast.Index`` 声明 ``value``，这里与调用图提取器一样按 3.8 的真实结构读取。
    """
    sliced = node.slice
    return sliced.value if isinstance(sliced, ast.Index) else sliced  # type: ignore[attr-defined]


def node_span(node: ast.AST) -> Tuple[int, int, int, int]:
    """节点在源码里的完整跨度 (起始行, 起始列, 结束行, 结束列)：起点相同的嵌套节点（``a + b == c`` 里的
    ``a + b`` 与整个比较）靠结束位置区分。"""
    assert isinstance(node, LOCATED)
    assert node.end_lineno is not None and node.end_col_offset is not None
    return node.lineno, node.col_offset, node.end_lineno, node.end_col_offset


def constant_int(node: ast.AST) -> Optional[int]:
    """整数字面量（含负号）的值；其他表达式返回 None。"""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = constant_int(node.operand)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    return None


def lambda_label(node: ast.Lambda) -> str:
    """与调用图提取器一致的 lambda 记录名。"""
    return f"<lambda>@{node.lineno}:{node.col_offset}"
