"""调用图函数/方法/嵌套 callable 与 import alias 的 AST 索引。纯标准库、Py3.8。"""
from __future__ import annotations

import ast
from typing import Any, Dict, List, Optional, Tuple

import callgraph_call_sites as _callsites

FuncInfo = Dict[str, Any]


def _end_lineno(node: ast.AST, fallback: int) -> int:
    return int(getattr(node, "end_lineno", fallback) or fallback)


def _function_info(
    rel: str,
    node: ast.AST,
    name: str,
    cls: Optional[str],
    *,
    scope: str,
    parent: Optional[str],
    nested: bool,
) -> FuncInfo:
    line = int(getattr(node, "lineno", 0) or 0)
    return {
        "qual": f"{rel}::{scope}",
        "rel": rel,
        "line": line,
        "end": _end_lineno(node, line),
        "cls": cls,
        "name": name,
        "scope": scope,
        "parent": parent,
        "nested": nested,
        "node": node,
    }


def _direct_nested_callables(node: ast.AST) -> List[ast.AST]:
    body = [node.body] if isinstance(node, ast.Lambda) else list(getattr(node, "body", []))
    stack: List[ast.AST] = list(reversed(body))
    callables: List[ast.AST] = []
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            callables.append(child)
            continue
        if isinstance(child, ast.ClassDef):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(child))))
    return callables


def _callable_label(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    return f"<lambda>@{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}"


def _nested_functions(
    rel: str,
    node: ast.AST,
    *,
    parent_scope: str,
    parent_qual: str,
    cls: Optional[str],
) -> List[FuncInfo]:
    functions: List[FuncInfo] = []
    for child in _direct_nested_callables(node):
        label = _callable_label(child)
        scope = f"{parent_scope}.<locals>.{label}"
        info = _function_info(
            rel,
            child,
            label,
            cls,
            scope=scope,
            parent=parent_qual,
            nested=True,
        )
        functions.append(info)
        functions.extend(
            _nested_functions(
                rel,
                child,
                parent_scope=scope,
                parent_qual=str(info["qual"]),
                cls=cls,
            )
        )
    return functions


def _module_functions(tree: ast.Module, rel: str) -> List[FuncInfo]:
    functions: List[FuncInfo] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        info = _function_info(rel, node, node.name, None, scope=node.name, parent=None, nested=False)
        functions.append(info)
        functions.extend(
            _nested_functions(
                rel,
                node,
                parent_scope=node.name,
                parent_qual=str(info["qual"]),
                cls=None,
            )
        )
    return functions


def _class_functions(tree: ast.Module, rel: str) -> List[FuncInfo]:
    functions: List[FuncInfo] = []
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            scope = f"{class_node.name}.{node.name}"
            info = _function_info(
                rel,
                node,
                node.name,
                class_node.name,
                scope=scope,
                parent=None,
                nested=False,
            )
            functions.append(info)
            functions.extend(
                _nested_functions(
                    rel,
                    node,
                    parent_scope=scope,
                    parent_qual=str(info["qual"]),
                    cls=class_node.name,
                )
            )
    return functions


def _module_name_from_rel(rel: str) -> str:
    normalized = rel.replace("\\", "/")
    return normalized[:-3].replace("/", ".") if normalized.endswith(".py") else normalized.replace("/", ".")


def _relative_import_module(rel: str, level: int, module: str) -> str:
    package_parts = _module_name_from_rel(rel).split(".")[:-1]
    if level > 1:
        package_parts = package_parts[: -(level - 1)]
    base = ".".join(package_parts)
    return f"{base}.{module}" if base and module else (module or base)


def _remember_import_aliases(imports: Dict[str, str], node: ast.AST, rel: str) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            imports[alias.asname or alias.name.split(".")[0]] = alias.name
    elif isinstance(node, ast.ImportFrom):
        module = _relative_import_module(rel, int(node.level or 0), node.module or "") if node.level else (node.module or "")
        for alias in node.names:
            imports[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name


def _import_aliases(tree: ast.Module, rel: str) -> Dict[str, str]:
    imports: Dict[str, str] = {}
    for node in tree.body:
        _remember_import_aliases(imports, node, rel)
    return imports


def function_import_aliases(fnode: ast.AST, rel: str) -> Dict[str, str]:
    """收集当前 callable 运行体内的 import alias，不吞入嵌套 callable。"""
    imports: Dict[str, str] = {}
    for node in _callsites.walk_runtime_body(fnode):
        _remember_import_aliases(imports, node, rel)
    return imports


def collect(tree: ast.Module, rel: str) -> Tuple[List[FuncInfo], Dict[str, str]]:
    return _module_functions(tree, rel) + _class_functions(tree, rel), _import_aliases(tree, rel)
