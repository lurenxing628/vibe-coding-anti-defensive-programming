"""AST 使用图的 import 和模块顶层绑定解析。"""
from __future__ import annotations

import ast

from .ast_nodes import before_position, position
from .scope import LocalAliases, bound_names_for_node


def resolve_import_aliases(context):
    for info in context["modules"].values():
        aliases = module_aliases_before(context, info.rel, None)
        info.module_aliases = dict(aliases.module_aliases)
        info.direct_aliases = dict(aliases.direct_aliases)
        info.class_aliases = dict(aliases.class_aliases)
        info.non_class_names = set(aliases.non_class_names)


def module_aliases_before(context, rel, before_line):
    cache_key = (rel, before_line)
    cached = context["module_alias_cache"].get(cache_key)
    if cached is not None:
        return cached
    info = context["modules"][rel]
    aliases = LocalAliases()
    for node in info.tree.body:
        if before_line is not None and position(node) > before_line:
            break
        if not before_position(node, before_line):
            continue
        _apply_module_node(context, aliases, info.module, info.rel, node)
    context["module_alias_cache"][cache_key] = aliases
    return aliases


def _apply_module_node(context, aliases, module_name, module_rel, node):
    if isinstance(node, ast.Import):
        for alias in node.names:
            _set_module_alias(aliases, alias.asname or alias.name.split(".")[0], alias.name)
    elif isinstance(node, ast.ImportFrom):
        base_module = resolve_import_from(module_name, node, module_rel)
        for alias in node.names:
            _bind_module_import_from(context, aliases, base_module, alias)
    elif isinstance(node, ast.ClassDef):
        class_id = context["class_by_module_name"].get(module_name, {}).get(node.name)
        if class_id:
            _set_class_alias(aliases, node.name, class_id)
        else:
            _set_non_class_binding(aliases, node.name)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _set_non_class_binding(aliases, node.name)
    else:
        for name in bound_names_for_node(node):
            _set_non_class_binding(aliases, name)


def resolve_import_from(current_module, node, current_rel=None):
    # type: (str, ast.ImportFrom, object) -> str
    if not node.level:
        return node.module or ""
    parts = current_module.split(".")
    package = parts if str(current_rel or "").endswith("/__init__.py") else parts[:-1]
    keep = max(0, len(package) - node.level + 1)
    base = package[:keep]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)

def _bind_module_import_from(context, info, base_module, alias):
    bound = alias.asname or alias.name
    candidate_module = base_module + "." + alias.name if base_module else alias.name
    if candidate_module in context["module_rel_by_name"]:
        _set_module_alias(info, bound, candidate_module)
        return
    qual = context["by_module_name"].get(base_module, {}).get(alias.name)
    if qual:
        _set_direct_alias(info, bound, qual)
        return
    class_id = context["class_by_module_name"].get(base_module, {}).get(alias.name)
    if class_id:
        _set_class_alias(info, bound, class_id)
        return
    _set_non_class_binding(info, bound)


def _clear_module_binding(info, name):
    info.module_aliases.pop(name, None)
    info.direct_aliases.pop(name, None)
    info.class_aliases.pop(name, None)
    info.non_class_names.discard(name)


def _set_module_alias(info, name, module):
    _clear_module_binding(info, name)
    info.module_aliases[name] = module
    info.non_class_names.add(name)


def _set_direct_alias(info, name, qual):
    _clear_module_binding(info, name)
    info.direct_aliases[name] = qual
    info.non_class_names.add(name)


def _set_class_alias(info, name, class_id):
    _clear_module_binding(info, name)
    info.class_aliases[name] = class_id


def _set_non_class_binding(info, name):
    _clear_module_binding(info, name)
    info.non_class_names.add(name)
