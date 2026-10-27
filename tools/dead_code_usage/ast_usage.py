"""轻量 AST 使用图。

目标不是重建精确调用链,而是回答一个更窄的问题:
候选函数是否被真实代码引用、注册、传递或通过可推断对象方法使用。
"""
from __future__ import annotations

import ast
import os
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .ast_nodes import (
    FunctionNode as _FunctionNode,
)
from .ast_nodes import (
    ModuleInfo as _ModuleInfo,
)
from .ast_nodes import (
    before_position as _before_position,
)
from .ast_nodes import (
    class_id as _class_id,
)
from .ast_nodes import (
    function_node_lookup as _function_node_lookup,
)
from .ast_nodes import (
    functions_by_rel as _functions_by_rel,
)
from .ast_nodes import (
    line as _line,
)
from .ast_nodes import (
    position as _position,
)
from .imports import (
    module_aliases_before as _module_aliases_before,
)
from .imports import (
    resolve_import_aliases as _resolve_import_aliases,
)
from .imports import (
    resolve_import_from as _resolve_import_from,
)
from .model import FunctionRecord, UsageEvidence, dedupe_evidence, module_name_from_rel, rel_join
from .scope import (
    iter_function_body_nodes as _iter_function_body_nodes,
)
from .scope import (
    iter_usage_nodes as _iter_usage_nodes,
)
from .scope import (
    local_aliases_before as _local_aliases_before,
)
from .scope import (
    local_events as _local_events,
)
from .scope import (
    local_scope_names as _local_scope_names,
)
from .type_hints import (
    annotation_name as _annotation_name,
)
from .type_hints import (
    function_return_types as _function_return_types,
)
from .type_hints import (
    initial_param_types as _initial_param_types,
)


def collect_ast_usage(repo_root, records):
    # type: (str, Dict[str, FunctionRecord]) -> Dict[str, List[UsageEvidence]]
    context = _build_context(repo_root, records)
    _resolve_import_aliases(context)
    context["return_types"] = _function_return_types(context, _class_id_for_name)
    context["param_types"] = _initial_param_types(context, _class_id, _class_id_for_name)
    _propagate_parameter_types(context)
    evidence = _collect_evidence(context)
    return _group_evidence(evidence)


def _build_context(repo_root, records):
    rels = _usage_rels(repo_root, records)
    modules = {}  # type: Dict[str, _ModuleInfo]
    module_by_rel = {}  # type: Dict[str, str]
    for rel in rels:
        path = rel_join(repo_root, rel)
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        try:
            tree = ast.parse(text, filename=rel, type_comments=True)
        except SyntaxError:
            continue
        module = module_name_from_rel(rel)
        modules[rel] = _ModuleInfo(rel, module, tree)
        module_by_rel[rel] = module

    function_nodes = {rel: _function_node_lookup(info.tree) for rel, info in modules.items()}
    functions = {}  # type: Dict[str, _FunctionNode]
    by_module_name = defaultdict(dict)  # type: Dict[str, Dict[str, str]]
    class_methods = defaultdict(dict)  # type: Dict[str, Dict[str, str]]
    class_by_module_name = defaultdict(dict)  # type: Dict[str, Dict[str, str]]
    class_names = defaultdict(set)  # type: Dict[str, Set[Tuple[str, str]]]
    for record in records.values():
        tree = modules.get(record.rel)
        if tree is None:
            continue
        node = function_nodes.get(record.rel, {}).get((record.line, record.name, record.cls))
        if node is None:
            continue
        functions[record.qual] = _FunctionNode(record, node)
        if record.cls:
            class_id = _class_id(record.rel, record.cls)
            class_methods[class_id][record.name] = record.qual
            class_by_module_name[module_by_rel[record.rel]][record.cls] = class_id
            class_names[record.cls].add((record.rel, record.cls))
        else:
            by_module_name[module_by_rel[record.rel]][record.name] = record.qual

    context = {
        "repo_root": repo_root,
        "records": records,
        "modules": modules,
        "module_by_rel": module_by_rel,
        "module_rel_by_name": {module: rel for rel, module in module_by_rel.items()},
        "functions": functions,
        "functions_by_rel": _functions_by_rel(functions),
        "local_events": _local_events(functions),
        "by_module_name": by_module_name,
        "class_methods": class_methods,
        "class_by_module_name": class_by_module_name,
        "class_ids_by_name": {
            name: {_class_id(rel, cls) for rel, cls in locations} for name, locations in class_names.items()
        },
    }
    context["return_types"] = {}
    context["param_types"] = defaultdict(lambda: defaultdict(set))
    context["local_type_cache"] = {}
    context["local_alias_cache"] = {}
    context["module_alias_cache"] = {}
    context["scope_name_cache"] = {}
    context["cache_local_types"] = False
    return context


def _usage_rels(repo_root, records):
    rels = set(record.rel for record in records.values())
    roots = set(rel.split("/", 1)[0] for rel in rels if "/" in rel)
    for root in roots:
        root_path = rel_join(repo_root, root)
        if not os.path.isdir(root_path):
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            for filename in filenames:
                if filename.endswith(".py"):
                    rels.add(os.path.relpath(os.path.join(dirpath, filename), repo_root).replace(os.sep, "/"))
    return sorted(rels)


def _class_id_for_name(context, name, rel, source_qual=None, before_line=None):
    # type: (Dict, Optional[str], str, Optional[str], object) -> Optional[str]
    if not name:
        return None
    if source_qual:
        aliases = _local_aliases_before(context, source_qual, before_line, _resolve_import_from)
        alias = aliases.class_aliases.get(name)
        if alias:
            return alias
        if name in _local_scope_names(context, source_qual):
            return None
    module_info = context["modules"].get(rel)
    if module_info is not None:
        if before_line is not None:
            aliases = _module_aliases_before(context, rel, before_line)
            alias = aliases.class_aliases.get(name)
            if alias:
                return alias
            if name in aliases.non_class_names:
                return None
        alias = module_info.class_aliases.get(name)
        if alias:
            return alias
        if name in module_info.non_class_names:
            return None
    ids = context["class_ids_by_name"].get(name) or set()
    local = _class_id(rel, name)
    if local in ids:
        return local
    if len(ids) == 1:
        return next(iter(ids))
    return None


def _propagate_parameter_types(context):
    for _round in range(4):
        context["local_type_cache"] = {}
        context["cache_local_types"] = True
        changed = False
        for qual, item in context["functions"].items():
            for call in _iter_calls(item.node):
                local_types = _local_var_types(context, qual, before_line=_position(call))
                target = _resolve_call_target(context, item.record.rel, call.func, source_qual=qual, before_line=_position(call))
                if target not in context["functions"]:
                    continue
                changed = _push_call_arg_types(context, item.record.rel, qual, target, call, local_types) or changed
        if not changed:
            context["cache_local_types"] = False
            return
    context["cache_local_types"] = False


def _iter_calls(node):
    for sub in _iter_function_body_nodes(node):
        if isinstance(sub, ast.Call):
            yield sub


def _push_call_arg_types(context, source_rel, source_qual, target_qual, call, local_types):
    target = context["functions"][target_qual]
    changed = False
    for index, arg in enumerate(call.args):
        if index >= len(target.params):
            continue
        type_name = _expr_type(context, arg, local_types, source_rel, source_qual, _position(call))
        if type_name and type_name not in context["param_types"][target_qual][target.params[index]]:
            context["param_types"][target_qual][target.params[index]].add(type_name)
            changed = True
    for keyword in call.keywords:
        if keyword.arg not in target.params:
            continue
        type_name = _expr_type(context, keyword.value, local_types, source_rel, source_qual, _position(call))
        if type_name and type_name not in context["param_types"][target_qual][keyword.arg]:
            context["param_types"][target_qual][keyword.arg].add(type_name)
            changed = True
    return changed


def _local_var_types(context, qual, before_line=None):
    cache_key = (qual, before_line)
    if context.get("cache_local_types"):
        cached = context["local_type_cache"].get(cache_key)
        if cached is not None:
            return cached
    item = context["functions"][qual]
    types = {name: next(iter(values)) for name, values in context["param_types"][qual].items() if len(values) == 1}
    for node in context["local_events"].get(qual, []):
        if not _before_position(node, before_line):
            break
        if isinstance(node, ast.Assign):
            type_name = _expr_type(context, node.value, types, item.record.rel, qual, _position(node))
            for target in node.targets:
                _set_local_type(types, target, type_name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = _annotation_name(node.annotation)
            class_id = _class_id_for_name(context, name, item.record.rel, qual, _position(node))
            type_name = class_id or _expr_type(context, node.value, types, item.record.rel, qual, _position(node))
            _set_local_type(types, node.target, type_name)
        elif isinstance(node, ast.AugAssign):
            _set_local_type(types, node.target, None)
        elif isinstance(node, ast.NamedExpr):
            type_name = _expr_type(context, node.value, types, item.record.rel, qual, _position(node))
            _set_local_type(types, node.target, type_name)
    if context.get("cache_local_types"):
        context["local_type_cache"][cache_key] = types
    return types


def _set_local_type(types, target, type_name):
    if isinstance(target, ast.Name):
        if type_name:
            types[target.id] = type_name
        else:
            types.pop(target.id, None)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            _set_local_type(types, item, None)


def _expr_type(context, expr, local_types, rel, source_qual=None, before_line=None):
    if expr is None:
        return None
    if isinstance(expr, ast.Name):
        return local_types.get(expr.id)
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name):
            class_id = _class_id_for_name(context, expr.func.id, rel, source_qual, before_line)
            if class_id:
                return class_id
        if isinstance(expr.func, ast.Attribute):
            class_id = _class_id_for_attribute(context, rel, expr.func, source_qual, before_line)
            if class_id:
                return class_id
        target = _resolve_call_target(context, rel, expr.func, source_qual=source_qual, before_line=before_line)
        if target:
            return context["return_types"].get(target)
    return None


def _class_id_for_attribute(context, rel, func, source_qual=None, before_line=None):
    if not isinstance(func.value, ast.Name):
        return None
    module = None
    if source_qual:
        aliases = _local_aliases_before(context, source_qual, before_line, _resolve_import_from)
        module = aliases.module_aliases.get(func.value.id)
        if not module and func.value.id in _local_scope_names(context, source_qual):
            return None
    if not module:
        if before_line is not None:
            aliases = _module_aliases_before(context, rel, before_line)
            module = aliases.module_aliases.get(func.value.id)
            if not module and func.value.id in aliases.non_class_names:
                return None
        if not module:
            module = context["modules"][rel].module_aliases.get(func.value.id)
    if module:
        return context["class_by_module_name"].get(module, {}).get(func.attr)
    return None


def _resolve_call_target(context, rel, func, source_qual=None, before_line=None):
    if isinstance(func, ast.Name):
        if source_qual:
            aliases = _local_aliases_before(context, source_qual, before_line, _resolve_import_from)
            direct = aliases.direct_aliases.get(func.id)
            if direct:
                return direct
            if func.id in _local_scope_names(context, source_qual):
                return None
        module = context["module_by_rel"].get(rel)
        if before_line is not None:
            aliases = _module_aliases_before(context, rel, before_line)
            direct = aliases.direct_aliases.get(func.id)
            if direct:
                return direct
        return context["modules"][rel].direct_aliases.get(func.id) or context["by_module_name"].get(module, {}).get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if source_qual:
            aliases = _local_aliases_before(context, source_qual, before_line, _resolve_import_from)
            module = aliases.module_aliases.get(func.value.id)
            if module:
                return context["by_module_name"].get(module, {}).get(func.attr)
            if func.value.id in _local_scope_names(context, source_qual):
                return None
        module = None
        if before_line is not None:
            aliases = _module_aliases_before(context, rel, before_line)
            module = aliases.module_aliases.get(func.value.id)
        if not module:
            module = context["modules"][rel].module_aliases.get(func.value.id)
        if module:
            return context["by_module_name"].get(module, {}).get(func.attr)
    return None


def _collect_evidence(context):
    evidence = []
    context["local_type_cache"] = {}
    context["cache_local_types"] = True
    for rel, module in context["modules"].items():
        evidence.extend(_framework_registration_evidence(context, rel, module))
        for node, hidden_names in _iter_usage_nodes(module.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id in hidden_names:
                    continue
                target = _resolve_name_reference(context, rel, node)
                if target:
                    evidence.extend(_evidence_for_node(context, target, rel, node, "name_reference"))
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id in hidden_names:
                    continue
                target = _resolve_attribute_reference(context, rel, node)
                if target:
                    evidence.extend(_evidence_for_node(context, target, rel, node, target[1]))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in hidden_names:
                    continue
                source_qual = _enclosing_function_qual(context, rel, _line(node))
                class_id = _class_id_for_name(context, node.func.id, rel, source_qual, _position(node))
                init_qual = context["class_methods"].get(class_id, {}).get("__init__")
                if init_qual:
                    evidence.extend(_evidence_for_node(context, init_qual, rel, node, "constructor_call"))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id in hidden_names:
                    continue
                source_qual = _enclosing_function_qual(context, rel, _line(node))
                class_id = _class_id_for_attribute(context, rel, node.func, source_qual, _position(node))
                init_qual = context["class_methods"].get(class_id, {}).get("__init__")
                if init_qual:
                    evidence.extend(_evidence_for_node(context, init_qual, rel, node, "constructor_call"))
    return dedupe_evidence(evidence)


def _resolve_name_reference(context, rel, node):
    name = node.id
    line = _line(node)
    before_pos = _position(node)
    module = context["module_by_rel"].get(rel)
    source_qual = _enclosing_function_qual(context, rel, line)
    if source_qual:
        aliases = _local_aliases_before(context, source_qual, before_pos, _resolve_import_from)
        direct = aliases.direct_aliases.get(name)
        if direct:
            return direct
        if name in _local_scope_names(context, source_qual):
            return None
    direct = context["modules"][rel].direct_aliases.get(name)
    if line is not None:
        aliases = _module_aliases_before(context, rel, before_pos)
        direct = aliases.direct_aliases.get(name)
    if direct:
        return direct
    return context["by_module_name"].get(module, {}).get(name)


def _resolve_attribute_reference(context, rel, node):
    if not isinstance(node.value, ast.Name):
        return None
    source_qual = _enclosing_function_qual(context, rel, _line(node))
    if source_qual:
        aliases = _local_aliases_before(context, source_qual, _position(node), _resolve_import_from)
        module = aliases.module_aliases.get(node.value.id)
        if module:
            qual = context["by_module_name"].get(module, {}).get(node.attr)
            return (qual, "module_attr_reference") if qual else None
        if node.value.id in _local_scope_names(context, source_qual):
            local_types = _local_var_types(context, source_qual, before_line=_position(node))
            type_name = local_types.get(node.value.id)
            if type_name:
                qual = context["class_methods"].get(type_name, {}).get(node.attr)
                return (qual, "typed_method_reference") if qual else None
            return None
    module = None
    aliases = _module_aliases_before(context, rel, _position(node))
    module = aliases.module_aliases.get(node.value.id)
    if not module and node.value.id in aliases.non_class_names:
        return None
    if not module:
        module = context["modules"][rel].module_aliases.get(node.value.id)
    if module:
        qual = context["by_module_name"].get(module, {}).get(node.attr)
        return (qual, "module_attr_reference") if qual else None
    local_types = _local_var_types(context, source_qual, before_line=_position(node)) if source_qual in context["functions"] else {}
    type_name = local_types.get(node.value.id)
    if type_name:
        qual = context["class_methods"].get(type_name, {}).get(node.attr)
        return (qual, "typed_method_reference") if qual else None
    return None


def _evidence_for_node(context, target, rel, node, kind):
    if isinstance(target, tuple):
        target = target[0]
    if not target:
        return []
    source_qual = _enclosing_function_qual(context, rel, _line(node))
    if source_qual == target:
        return []
    return [UsageEvidence(target=target, source_rel=rel, line=_line(node), kind=kind, detail=source_qual or "<module>")]


# Flask/Jinja 框架注册装饰器:被这些装饰的函数在装饰器求值时就被框架注册,
# 调用方是框架本身,AST 使用图里看不到调用边,不算死代码。
_FRAMEWORK_REGISTRATION_DECORATORS = frozenset({
    "route", "get", "post", "put", "patch", "delete", "add_url_rule",
    "context_processor", "template_filter", "template_global", "template_test",
})


def _framework_registration_evidence(context, rel, module):
    # type: (dict, str, _ModuleInfo) -> List[UsageEvidence]
    """为被框架注册装饰器装饰的函数补"已注册"证据,消路由/模板钩子误报。

    只豁免紧邻装饰器的函数本体,不按目录一刀切——同文件里的普通业务函数
    没有装饰器、拿不到证据,真死代码时仍会被报出。
    """
    found = []
    tree = module.tree
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not _is_framework_registration_decorator(deco):
                continue
            target = context["by_module_name"].get(module.module, {}).get(node.name)
            if target:
                found.append(UsageEvidence(
                    target=target, source_rel=rel, line=_line(node),
                    kind="framework_registration", detail="<decorator>",
                ))
    return found


def _is_framework_registration_decorator(deco):
    # type: (ast.expr) -> bool
    """判断装饰器是否为框架注册型,如 @bp.post("/x")、@app.context_processor。

    形态限定为「对象属性调用」或「裸属性」,即 @xxx.<name>(...) 或 @xxx.<name>;
    装饰器工厂的返回对象再注册(如 @bp.route(...)(other))不在此列。
    """
    target = deco.func if isinstance(deco, ast.Call) else deco
    if isinstance(target, ast.Attribute):
        return target.attr in _FRAMEWORK_REGISTRATION_DECORATORS
    return False


def _enclosing_function_qual(context, rel, line):
    best = None
    best_span = 10 ** 9
    for item in context["functions_by_rel"].get(rel, []):
        rec = item.record
        if rec.line <= line <= rec.end and rec.end - rec.line < best_span:
            best = rec.qual
            best_span = rec.end - rec.line
    if best is not None:
        return best
    best_span = 10 ** 9
    for item in context["functions_by_rel"].get(rel, []):
        start = int(getattr(item.node, "lineno", 0) or 0)
        end = int(getattr(item.node, "end_lineno", start) or start)
        if start <= line <= end and end - start < best_span:
            best = item.record.qual
            best_span = end - start
    return best


def _group_evidence(evidence):
    grouped = defaultdict(list)
    for item in evidence:
        grouped[item.target].append(item)
    return {target: list(items) for target, items in grouped.items()}
