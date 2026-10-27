"""调用图 AST 调用点提取。

属性调用保留完整接收者文本和行号，避免在边消解前把 ``self.repo.get()``、
``repo.get()`` 和 ``self.get()`` 都压成同一个 ``get``。纯标准库、Py3.8。
"""
from __future__ import annotations

import ast
from typing import Iterator, List, Set, Tuple

BareCalls = Set[str]
AttrCalls = Set[Tuple[str, str, int]]
ModuleAttrCalls = Set[Tuple[str, str, int]]


def receiver_text(node: ast.AST) -> str:
    """返回保守、稳定的接收者表达；不猜无法静态识别的运行时对象。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = receiver_text(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        callee = receiver_text(node.func)
        return f"{callee}()" if callee else "<call>()"
    if isinstance(node, ast.Subscript):
        base = receiver_text(node.value)
        return f"{base}[]" if base else "<subscript>"
    return f"<{node.__class__.__name__}>"


def call_name(func: ast.AST) -> Tuple[str, str]:
    if isinstance(func, ast.Name):
        return func.id, "bare"
    if isinstance(func, ast.Attribute):
        return func.attr, "attr"
    return "", ""


def walk_runtime_body(fnode: ast.AST) -> Iterator[ast.AST]:
    """遍历当前 callable 的运行体，但不把嵌套 callable/class 的执行体算给外层。"""
    if isinstance(fnode, ast.Lambda):
        body = [fnode.body]
    else:
        body = list(getattr(fnode, "body", []))
    stack: List[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def call_argument_references(fnode: ast.AST) -> Set[str]:
    references: Set[str] = set()
    for node in walk_runtime_body(fnode):
        if not isinstance(node, ast.Call):
            continue
        references.update(arg.id for arg in node.args if isinstance(arg, ast.Name))
        references.update(
            keyword.value.id for keyword in node.keywords if isinstance(keyword.value, ast.Name)
        )
    return references


def collect_calls(fnode: ast.AST) -> Tuple[BareCalls, AttrCalls, Set[str], ModuleAttrCalls]:
    """收集调用；属性调用以 ``(完整接收者, 方法名, 行号)`` 保存。"""
    bare: BareCalls = set()
    attr: AttrCalls = set()
    dynamic: Set[str] = set()
    module_attr: ModuleAttrCalls = set()
    for node in walk_runtime_body(fnode):
        if not isinstance(node, ast.Call):
            continue
        name, kind = call_name(node.func)
        if not name:
            continue
        if kind == "bare":
            bare.add(name)
        else:
            line = int(getattr(node, "lineno", 0) or 0)
            receiver = receiver_text(node.func.value)  # type: ignore[union-attr]
            attr.add((receiver, name, line))
            if isinstance(node.func.value, ast.Name):  # type: ignore[union-attr]
                module_attr.add((node.func.value.id, name, line))  # type: ignore[union-attr]
        if name in ("getattr", "__import__", "import_module"):
            dynamic.add(name)
    return bare, attr, dynamic, module_attr
