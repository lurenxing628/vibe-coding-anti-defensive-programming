"""调用图敏感值共现与风险数据流分析 leaf helper。纯标准库、Py3.8。"""
from __future__ import annotations

import ast
from typing import Any, Dict, List, Set

import callgraph_call_sites as _callsites

HARDCODED_HINT = ("RESOLUTION", "DEFAULT", "ADOPTED", "FALLBACK", "PLACEHOLDER", "DUMMY", "STUB")
SENSITIVE = (
    "scenario_id",
    "plan_role",
    "requested_role",
    "is_preview",
    "is_simulation",
    "version",
    "plan_resolution",
    "is_superseded_by_newer_version",
)
SINK_WRITE = ("execute", "executemany", "insert", "update", "delete", "commit", "save", "write", "create")
SINK_RENDER = ("render", "to_dict", "jsonify", "make_response")
SINK_RESOLVE = ("resolve_plan", "resolve_version", "resolve_plan_view", "build_plan_identity")


def _remember_identifier(name: str, sensitive: Set[str], hardcoded: Set[str]) -> None:
    if name in SENSITIVE:
        sensitive.add(name)
    if name.isupper() and any(hint in name for hint in HARDCODED_HINT):
        hardcoded.add(name)


def _call_flow(name: str, writes: Set[str], renders: Set[str], resolves: Set[str]) -> None:
    lowered = name.lower()
    if any(hint in lowered for hint in SINK_WRITE):
        writes.add(name)
    if any(hint in lowered for hint in SINK_RENDER):
        renders.add(name)
    if any(hint in name for hint in SINK_RESOLVE):
        resolves.add(name)


def collect(fnode: ast.AST) -> Dict[str, List[str]]:
    sensitive, hardcoded, writes, renders, resolves = set(), set(), set(), set(), set()
    for node in _callsites.walk_runtime_body(fnode):
        if isinstance(node, ast.Name):
            _remember_identifier(node.id, sensitive, hardcoded)
        elif isinstance(node, ast.Attribute):
            _remember_identifier(node.attr, sensitive, hardcoded)
        elif isinstance(node, ast.Call):
            name, _kind = _callsites.call_name(node.func)
            _call_flow(name, writes, renders, resolves)
    return {
        "src_sensitive": sorted(sensitive),
        "src_hardcoded": sorted(hardcoded),
        "sinks_write": sorted(writes),
        "sinks_render": sorted(renders),
        "sinks_resolve": sorted(resolves),
    }


def _risk_reasons(dataflow: Dict[str, List[str]]) -> List[str]:
    reasons: List[str] = []
    if dataflow["src_hardcoded"] and (dataflow["sinks_render"] or dataflow["sinks_resolve"]):
        reasons.append("hardcoded->render/resolve(P1假冒嫌疑)")
    if dataflow["src_sensitive"] and dataflow["sinks_write"]:
        reasons.append("sensitive-identity->write(越权写入嫌疑)")
    preview_sources = ("scenario_id", "is_preview", "is_simulation")
    if any(source in dataflow["src_sensitive"] for source in preview_sources) and dataflow["sinks_render"]:
        reasons.append("preview->render(预览泄漏嫌疑)")
    return reasons


def risk_rows(
    dataflow_nodes: Dict[str, Dict[str, List[str]]],
    all_funcs: Dict[str, Dict[str, Any]],
    fan_in: Dict[str, int],
    fan_out: Dict[str, int],
) -> List[Dict[str, Any]]:
    risk: List[Dict[str, Any]] = []
    for qual, dataflow in dataflow_nodes.items():
        reasons = _risk_reasons(dataflow)
        if not reasons:
            continue
        info = all_funcs[qual]
        risk.append(
            {
                "func": qual,
                "rel": info["rel"],
                "line": info["line"],
                "fan_in": fan_in.get(qual, 0),
                "fan_out": fan_out.get(qual, 0),
                "co_occurrence": reasons,
                **dataflow,
            }
        )
    return sorted(
        risk,
        key=lambda item: (-len(item["co_occurrence"]), -item["fan_in"], item["rel"], item["line"], item["func"]),
    )
