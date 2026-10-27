"""AST 使用图的类型注解读取。"""
from __future__ import annotations

import ast
import re
from collections import defaultdict

_RETURN_COMMENT = re.compile(r"->\s*([A-Za-z_][A-Za-z0-9_.]*)")


def function_return_types(context, class_id_for_name):
    result = {}
    for qual, item in context["functions"].items():
        result[qual] = _return_type(context, item.record.rel, item.node, class_id_for_name)
    return result


def _return_type(context, rel, node, class_id_for_name):
    annotation = getattr(node, "returns", None)
    name = annotation_name(annotation)
    if name:
        return class_id_for_name(context, name, rel)
    type_comment = getattr(node, "type_comment", None)
    if type_comment:
        matched = _RETURN_COMMENT.search(type_comment)
        if matched:
            return class_id_for_name(context, matched.group(1).split(".")[-1], rel)
    return None


def annotation_name(node):
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip("'\"").split(".")[-1]
    if isinstance(node, ast.Subscript):
        base = node.value
        base_name = getattr(base, "id", None) or getattr(base, "attr", None)
        if base_name == "Optional":
            sliced = node.slice
            inner = getattr(sliced, "value", sliced)
            return annotation_name(inner)
    return None


def initial_param_types(context, class_id, class_id_for_name):
    param_types = defaultdict(lambda: defaultdict(set))
    for qual, item in context["functions"].items():
        record = item.record
        if record.cls and item.params:
            param_types[qual][item.params[0]].add(class_id(record.rel, record.cls))
        args = getattr(item.node, "args", None)
        if args is None:
            continue
        for arg in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
            name = annotation_name(arg.annotation)
            resolved = class_id_for_name(context, name, record.rel)
            if resolved:
                param_types[qual][arg.arg].add(resolved)
    return param_types
