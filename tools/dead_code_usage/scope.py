"""AST 使用图的 Python 名字作用域规则。"""
from __future__ import annotations

import ast
from typing import Dict, Set

from .ast_nodes import before_position, position


class LocalAliases:
    def __init__(self):
        self.module_aliases = {}  # type: Dict[str, str]
        self.direct_aliases = {}  # type: Dict[str, str]
        self.class_aliases = {}  # type: Dict[str, str]
        self.non_class_names = set()  # type: Set[str]


def iter_function_body_nodes(node):
    stack = list(reversed(getattr(node, "body", [])))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def iter_usage_nodes(tree):
    yield from _iter_usage_nodes(tree, set())


def _iter_usage_nodes(node, hidden):
    yield node, hidden
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        comp_hidden = set(hidden)
        for generator in node.generators:
            yield from _iter_usage_nodes(generator.iter, comp_hidden)
            comp_hidden = set(comp_hidden)
            comp_hidden.update(bound_names_for_target(generator.target))
            for condition in generator.ifs:
                yield from _iter_usage_nodes(condition, comp_hidden)
        if isinstance(node, ast.DictComp):
            yield from _iter_usage_nodes(node.key, comp_hidden)
            yield from _iter_usage_nodes(node.value, comp_hidden)
        else:
            yield from _iter_usage_nodes(node.elt, comp_hidden)
        return
    for child in ast.iter_child_nodes(node):
        yield from _iter_usage_nodes(child, hidden)


def local_events(functions):
    events = {}
    for qual, item in functions.items():
        nodes = []
        for node in iter_function_body_nodes(item.node):
            if isinstance(
                node,
                (
                    ast.Assign,
                    ast.AnnAssign,
                    ast.AugAssign,
                    ast.For,
                    ast.AsyncFor,
                    ast.With,
                    ast.Import,
                    ast.ImportFrom,
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.NamedExpr,
                ),
            ):
                nodes.append(node)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                nodes.append(node)
        nodes.sort(key=position)
        events[qual] = nodes
    return events


def local_scope_names(context, qual):
    cached = context["scope_name_cache"].get(qual)
    if cached is not None:
        return cached
    item = context["functions"][qual]
    names = set(item.params)
    for node in context["local_events"].get(qual, []):
        names.update(bound_names_for_node(node))
    context["scope_name_cache"][qual] = names
    return names


def local_aliases_before(context, qual, before_line, resolve_import_from):
    cache_key = (qual, before_line)
    cached = context["local_alias_cache"].get(cache_key)
    if cached is not None:
        return cached
    item = context["functions"][qual]
    info = context["modules"][item.record.rel]
    aliases = LocalAliases()
    for node in context["local_events"].get(qual, []):
        if before_line is not None and position(node) > before_line:
            break
        if not before_position(node, before_line):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                _clear_alias(aliases, bound)
                aliases.module_aliases[bound] = alias.name
        elif isinstance(node, ast.ImportFrom):
            base_module = resolve_import_from(info.module, node, info.rel)
            for alias in node.names:
                _bind_import_from(context, aliases, base_module, alias)
        else:
            for name in bound_names_for_node(node):
                _clear_alias(aliases, name)
    context["local_alias_cache"][cache_key] = aliases
    return aliases


def bound_names_for_node(node):
    names = set()  # type: Set[str]
    if isinstance(node, ast.Assign):
        for target in node.targets:
            names.update(bound_names_for_target(target))
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        names.update(bound_names_for_target(node.target))
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        names.update(bound_names_for_target(node.target))
    elif isinstance(node, ast.With):
        for item_node in node.items:
            if item_node.optional_vars is not None:
                names.update(bound_names_for_target(item_node.optional_vars))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.ExceptHandler) and node.name:
        names.add(str(node.name))
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    return names


def bound_names_for_target(target):
    names = set()  # type: Set[str]
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            names.update(bound_names_for_target(item))
    return names


def _bind_import_from(context, aliases, base_module, alias):
    bound = alias.asname or alias.name
    _clear_alias(aliases, bound)
    candidate_module = base_module + "." + alias.name if base_module else alias.name
    if candidate_module in context["module_rel_by_name"]:
        aliases.module_aliases[bound] = candidate_module
        return
    qual = context["by_module_name"].get(base_module, {}).get(alias.name)
    if qual:
        aliases.direct_aliases[bound] = qual
        return
    class_id = context["class_by_module_name"].get(base_module, {}).get(alias.name)
    if class_id:
        aliases.class_aliases[bound] = class_id


def _clear_alias(aliases, name):
    aliases.module_aliases.pop(name, None)
    aliases.direct_aliases.pop(name, None)
    aliases.class_aliases.pop(name, None)
    aliases.non_class_names.discard(name)
