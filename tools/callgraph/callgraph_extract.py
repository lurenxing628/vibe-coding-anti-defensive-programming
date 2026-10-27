#!/usr/bin/env python3
"""函数级调用图 + 关键值数据流追踪。

补足模块级 import 图看不见的：函数粒度的扇入扇出/环/桥接/孤岛 + 危险值源头流向。
嫌疑清单由图算法机械生成（消除 Agent 显著性偏差），Agent 只核验不发现。
调用消解保守+标注：能确定的画实边，不能确定的标 ambiguous，动态调用单列不画假边。
只读源码。产物默认写到 .checkup/callgraph/（可通过 CHECKUP_CALLGRAPH 覆盖）。
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT_DIR = os.path.abspath(os.environ.get("CHECKUP_CALLGRAPH") or os.path.join(REPO_ROOT, ".checkup", "callgraph"))

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import callgraph_call_sites as _callsites  # 保留完整接收者的调用点提取 leaf helper
import callgraph_dataflow as _flow  # 敏感值共现与风险数据流 leaf helper
import callgraph_function_index as _findex  # callable/import alias AST 索引 leaf helper
import callgraph_type_resolver as _tresolver  # 两档类型消解（Tier 1 AST + Tier 2 SCIP/Runtime）

FIRST_PARTY_ROOTS = ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop")
EXCLUDE = {".git", "__pycache__", ".venv", "venv", "node_modules", "vendor",
           "backups", "evidence", ".ruff_cache", ".pytest_cache", "dist", "build"}

FuncInfo = Dict[str, Any]
IndexResult = Tuple[
    Dict[str, FuncInfo],
    Dict[str, List[str]],
    Dict[str, List[FuncInfo]],
    Dict[str, Dict[str, str]],
    Dict[str, ast.Module],
    List[str],
]
EdgeResult = Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, List[str]]]]


def _rel(p: str) -> str:
    return os.path.relpath(p, REPO_ROOT).replace("\\", "/")


def _iter_py() -> List[str]:
    out: List[str] = list(glob.glob(os.path.join(REPO_ROOT, "*.py")))
    for root in FIRST_PARTY_ROOTS:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in EXCLUDE]
            out.extend(os.path.join(dp, fn) for fn in fns if fn.endswith(".py"))
    return sorted(out)


_collect = _findex.collect


def _parse_file(fp: str, rel: str) -> Tuple[Optional[ast.Module], str]:
    try:
        with open(fp, encoding="utf-8") as fh:
            return ast.parse(fh.read(), filename=rel), ""
    except (OSError, UnicodeError, SyntaxError) as exc:
        return None, f"{rel}: {exc}"


def _index_codebase() -> IndexResult:
    all_funcs: Dict[str, FuncInfo] = {}
    name_to_quals: Dict[str, List[str]] = defaultdict(list)
    file_funcs: Dict[str, List[FuncInfo]] = defaultdict(list)
    file_imports: Dict[str, Dict[str, str]] = {}
    trees: Dict[str, ast.Module] = {}
    parse_errors: List[str] = []

    for fp in _iter_py():
        rel = _rel(fp)
        tree, error = _parse_file(fp, rel)
        if error:
            parse_errors.append(error)
            continue
        if tree is not None:
            trees[rel] = tree
        funcs, imports = _collect(tree, rel) if tree is not None else ([], {})
        file_imports[rel] = imports
        for info in funcs:
            all_funcs[info["qual"]] = info
            name_to_quals[info["name"]].append(info["qual"])
            file_funcs[rel].append(info)
    return all_funcs, name_to_quals, file_funcs, file_imports, trees, parse_errors


def _local_function_targets(
    file_funcs: Dict[str, List[FuncInfo]],
    info: FuncInfo,
) -> Dict[str, List[str]]:
    functions = file_funcs[info["rel"]]
    tiers: List[List[FuncInfo]] = [
        [function for function in functions if function.get("parent") == info["qual"]],
    ]
    if info.get("parent"):
        tiers.append(
            [function for function in functions if function.get("parent") == info.get("parent")]
        )
    tiers.append(
        [
            function
            for function in functions
            if function["cls"] is None and not function.get("nested")
        ]
    )
    targets: Dict[str, List[str]] = {}
    for tier in tiers:
        grouped: Dict[str, List[str]] = defaultdict(list)
        for function in tier:
            grouped[str(function["name"])].append(str(function["qual"]))
        for name, rows in grouped.items():
            targets.setdefault(name, sorted(set(rows)))
    return targets


def _method_names(file_funcs: Dict[str, List[FuncInfo]], info: FuncInfo) -> Set[str]:
    if not info["cls"]:
        return set()
    return {
        f["name"]
        for f in file_funcs[info["rel"]]
        if f["cls"] == info["cls"] and not f.get("nested")
    }


def _edge(source: str, target: str, kind: str, ambiguous: bool) -> Dict[str, Any]:
    return {"from": source, "to": target, "kind": kind, "ambiguous": ambiguous}


def _edge_key(edge: Dict[str, Any]) -> Tuple[str, str, str, bool]:
    return (str(edge["from"]), str(edge["to"]), str(edge["kind"]), bool(edge["ambiguous"]))


def _add_bare_edges(
    edges: List[Dict[str, Any]],
    qual: str,
    names: Set[str],
    local: Dict[str, List[str]],
    name_to_quals: Dict[str, List[str]],
    imports: Dict[str, str],
    all_funcs: Dict[str, FuncInfo],
) -> None:
    for name in sorted(names):
        imported = imports.get(name)
        if imported:
            module, separator, attr = imported.rpartition(".")
            target = _module_attr_target(module, attr, all_funcs) if separator else None
            if target:
                edges.append(_edge(qual, target, "import", False))
            continue
        local_candidates = local.get(name, [])
        if len(local_candidates) == 1:
            edges.append(_edge(qual, local_candidates[0], "local", False))
            continue
        if len(local_candidates) > 1:
            edges.extend(_edge(qual, candidate, "local", True) for candidate in local_candidates)
            continue
        candidates = sorted(name_to_quals.get(name, []))
        if len(candidates) == 1:
            edges.append(_edge(qual, candidates[0], "name", False))
        elif len(candidates) > 1:
            edges.extend(_edge(qual, candidate, "name", True) for candidate in candidates)


def _add_attr_edges(edges: List[Dict[str, Any]], qual: str, info: FuncInfo,
                    calls: Set[Tuple[str, str, int]], methods: Set[str],
                    name_to_quals: Dict[str, List[str]],
                    all_funcs: Dict[str, FuncInfo]) -> None:
    self_methods: Set[str] = set()
    unresolved_names: Set[str] = set()
    for receiver, name, _line in sorted(calls):
        if receiver == "self" and name in methods:
            self_methods.add(name)
        else:
            unresolved_names.add(name)
    for name in sorted(self_methods):
        edges.append(_edge(qual, f"{info['rel']}::{info['cls']}.{name}", "self", False))
    for name in sorted(unresolved_names):
        candidates = sorted(
            candidate for candidate in name_to_quals.get(name, [])
            if candidate != qual and all_funcs[candidate].get("cls") is not None
        )
        if 1 <= len(candidates) <= 6:
            edges.extend(_edge(qual, candidate, "attr", True) for candidate in candidates)


def _module_attr_target(module: str, attr: str, all_funcs: Dict[str, FuncInfo]) -> Optional[str]:
    candidates = [
        f"{module.replace('.', '/')}.py::{attr}",
        f"{module.replace('.', '/')}/__init__.py::{attr}",
    ]
    for candidate in candidates:
        if candidate in all_funcs:
            return candidate
    return None


def _add_imported_module_attr_edges(edges: List[Dict[str, Any]], qual: str,
                                    calls: Set[Tuple[str, str, int]], imports: Dict[str, str],
                                    all_funcs: Dict[str, FuncInfo]) -> Set[Tuple[str, str, int]]:
    resolved_pairs: Set[Tuple[str, str]] = set()
    for base, attr in sorted({(base, attr) for base, attr, _line in calls}):
        module = imports.get(base)
        if not module:
            continue
        target = _module_attr_target(module, attr, all_funcs)
        if target:
            edges.append(_edge(qual, target, "module_import_attr", False))
            resolved_pairs.add((base, attr))
    return {call for call in calls if (call[0], call[1]) in resolved_pairs}


def _add_function_reference_edges(edges: List[Dict[str, Any]], qual: str, names: Set[str],
                                  local: Dict[str, List[str]], name_to_quals: Dict[str, List[str]]) -> None:
    for name in sorted(names):
        if name in local:
            edges.extend(_edge(qual, target, "function_reference", True) for target in local[name])
            continue
        candidates = sorted(name_to_quals.get(name, []))
        if len(candidates) == 1:
            edges.append(_edge(qual, candidates[0], "function_reference", True))


def _resolve_edges(all_funcs: Dict[str, FuncInfo], name_to_quals: Dict[str, List[str]],
                   file_funcs: Dict[str, List[FuncInfo]], file_imports: Dict[str, Dict[str, str]],
                   trees: Dict[str, ast.Module]) -> EdgeResult:
    edges: List[Dict[str, Any]] = []
    dynamic_unresolved: List[Dict[str, Any]] = []
    dataflow_nodes: Dict[str, Dict[str, List[str]]] = {}
    resolver = _tresolver.TypeResolver(trees, all_funcs, file_imports)
    for qual, info in sorted(all_funcs.items()):
        if info.get("nested") and isinstance(info["node"], ast.Lambda) and info.get("parent") in all_funcs:
            edges.append(_edge(str(info["parent"]), qual, "lambda_definition", True))
        bare, attr, dyn, module_attr = _callsites.collect_calls(info["node"])
        references = _callsites.call_argument_references(info["node"]) - bare
        dataflow = _flow.collect(info["node"])
        if any(dataflow.values()):
            dataflow_nodes[qual] = dataflow
        if dyn:
            dynamic_unresolved.append({"func": qual, "rel": info["rel"], "line": info["line"], "hints": sorted(dyn)})
        local_targets = _local_function_targets(file_funcs, info)
        imports = dict(file_imports.get(info["rel"], {}))
        imports.update(_findex.function_import_aliases(info["node"], info["rel"]))
        _add_bare_edges(
            edges,
            qual,
            bare,
            local_targets,
            name_to_quals,
            imports,
            all_funcs,
        )
        _add_function_reference_edges(edges, qual, references, local_targets, name_to_quals)
        resolved_module_attr = _add_imported_module_attr_edges(edges, qual, module_attr, imports, all_funcs)
        remaining_attr = attr - resolved_module_attr
        typed_edges, resolved_typed = resolver.resolve_attr_calls(qual, info, remaining_attr, imports)
        edges.extend(typed_edges)
        _add_attr_edges(
            edges,
            qual,
            info,
            remaining_attr - resolved_typed,
            _method_names(file_funcs, info),
            name_to_quals,
            all_funcs,
        )
    return edges, dynamic_unresolved, dataflow_nodes


def _fan_counts(edges: List[Dict[str, Any]]) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int]]:
    fan_out: Dict[str, int] = defaultdict(int)
    fan_in: Dict[str, int] = defaultdict(int)
    ambiguous_fan_out: Dict[str, int] = defaultdict(int)
    ambiguous_fan_in: Dict[str, int] = defaultdict(int)
    for edge in edges:
        if edge["ambiguous"]:
            ambiguous_fan_out[edge["from"]] += 1
            ambiguous_fan_in[edge["to"]] += 1
            continue
        fan_out[edge["from"]] += 1
        fan_in[edge["to"]] += 1
    return fan_in, fan_out, ambiguous_fan_in, ambiguous_fan_out


def _write_json(name: str, obj: Any) -> None:
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def _cycle_key(cycle: List[str]) -> List[str]:
    if not cycle:
        return []
    smallest = min(range(len(cycle)), key=lambda idx: cycle[idx])
    return cycle[smallest:] + cycle[:smallest]


def _bounded_cycles(nx: Any, graph: Any) -> List[List[str]]:
    cycles: List[List[str]] = []
    for cycle in nx.simple_cycles(graph):
        if 2 <= len(cycle) <= 8:
            cycles.append(_cycle_key(list(cycle)))
        if len(cycles) >= 200:
            break
    return sorted(cycles)


def _articulation_points(nx: Any, graph: Any) -> List[str]:
    undirected = graph.to_undirected()
    if not undirected.number_of_nodes():
        return []
    largest = max(nx.connected_components(undirected), key=len)
    return list(nx.articulation_points(undirected.subgraph(largest)))


def _graph_metrics(all_funcs: Dict[str, FuncInfo], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        import networkx as nx
    except ImportError as exc:
        raise RuntimeError("缺少 networkx，不能生成可信的调用图指标。") from exc

    graph = nx.DiGraph()
    total_graph = nx.DiGraph()
    graph.add_nodes_from(all_funcs.keys())
    total_graph.add_nodes_from(all_funcs.keys())
    graph.add_edges_from((edge["from"], edge["to"]) for edge in edges if not edge["ambiguous"])
    total_graph.add_edges_from((edge["from"], edge["to"]) for edge in edges)
    cycles = _bounded_cycles(nx, graph)
    islands = [node for node in total_graph.nodes if total_graph.in_degree(node) == 0 and total_graph.out_degree(node) == 0]
    articulation = _articulation_points(nx, graph)
    _write_json("cycles.json", cycles)
    _write_json("islands.json", sorted(islands))
    _write_json("articulation_points.json", sorted(articulation))
    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "edge_count_confident": graph.number_of_edges(),
        "edge_count_total": total_graph.number_of_edges(),
        "cycle_count": len(cycles),
        "cycle_count_semantics": "confident-edge simple cycles of length 2..8, capped at 200",
        "island_count": len(islands),
        "articulation_count": len(articulation),
    }


def _high_fan_in(all_funcs: Dict[str, FuncInfo], fan_in: Dict[str, int]) -> List[Dict[str, Any]]:
    rows = (
        {"func": qual, "rel": all_funcs[qual]["rel"], "line": all_funcs[qual]["line"], "fan_in": count}
        for qual, count in fan_in.items() if count >= 5
    )
    return sorted(rows, key=lambda item: (-item["fan_in"], item["rel"], item["line"], item["func"]))[:60]


def _functions_payload(
    all_funcs: Dict[str, FuncInfo],
    fan_in: Dict[str, int],
    fan_out: Dict[str, int],
    ambiguous_fan_in: Dict[str, int],
    ambiguous_fan_out: Dict[str, int],
) -> Dict[str, Dict[str, Any]]:
    return {
        qual: {"rel": info["rel"], "line": info["line"], "end": info["end"], "cls": info["cls"],
               "name": info["name"],
               "fan_in": fan_in.get(qual, 0) + ambiguous_fan_in.get(qual, 0),
               "fan_out": fan_out.get(qual, 0) + ambiguous_fan_out.get(qual, 0),
               "fan_in_confident": fan_in.get(qual, 0),
               "fan_out_confident": fan_out.get(qual, 0),
               "fan_in_ambiguous": ambiguous_fan_in.get(qual, 0),
               "fan_out_ambiguous": ambiguous_fan_out.get(qual, 0)}
        for qual, info in all_funcs.items()
    }


def _summary(all_funcs: Dict[str, FuncInfo], edges: List[Dict[str, Any]],
             dynamic_unresolved: List[Dict[str, Any]], dataflow_nodes: Dict[str, Dict[str, List[str]]],
             risk: List[Dict[str, Any]], high_fan_in: List[Dict[str, Any]],
             graph_metrics: Dict[str, Any], parse_errors: List[str]) -> Dict[str, Any]:
    return {
        "total_functions": len(all_funcs),
        "total_edges": len(edges),
        "confident_edges": sum(1 for edge in edges if not edge["ambiguous"]),
        "ambiguous_edges": sum(1 for edge in edges if edge["ambiguous"]),
        "typed_edges": sum(1 for edge in edges if edge["kind"] == "typed"),
        "dynamic_unresolved_sites": len(dynamic_unresolved),
        "dataflow_flagged_funcs": len(dataflow_nodes),
        "risk_dataflow_count": len(risk),
        "high_fan_in_count": len(high_fan_in),
        "graph_metrics": graph_metrics,
        "parse_errors": parse_errors,
    }


def _write_outputs(all_funcs: Dict[str, FuncInfo], edges: List[Dict[str, Any]],
                   dynamic_unresolved: List[Dict[str, Any]], dataflow_nodes: Dict[str, Dict[str, List[str]]],
                   fan_in: Dict[str, int], fan_out: Dict[str, int],
                   ambiguous_fan_in: Dict[str, int], ambiguous_fan_out: Dict[str, int],
                   parse_errors: List[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    edges = sorted(edges, key=_edge_key)
    dynamic_unresolved = sorted(dynamic_unresolved, key=lambda item: (item["rel"], item["line"], item["func"]))
    graph_metrics = _graph_metrics(all_funcs, edges)
    risk = _flow.risk_rows(dataflow_nodes, all_funcs, fan_in, fan_out)
    high_fan_in = _high_fan_in(all_funcs, fan_in)
    summary = _summary(all_funcs, edges, dynamic_unresolved, dataflow_nodes, risk, high_fan_in, graph_metrics, parse_errors)
    _write_json("functions.json", _functions_payload(all_funcs, fan_in, fan_out, ambiguous_fan_in, ambiguous_fan_out))
    _write_json("edges.json", edges)
    _write_json("dynamic_unresolved.json", dynamic_unresolved)
    _write_json("dataflow_nodes.json", dataflow_nodes)
    _write_json("risk_dataflow.json", risk)
    _write_json("high_fan_in.json", high_fan_in)
    _write_json("summary.json", summary)
    return summary, risk


def _raise_parse_errors(parse_errors: List[str]) -> None:
    if not parse_errors:
        return
    details = "\n".join(f"- {error}" for error in parse_errors[:20])
    if len(parse_errors) > 20:
        details += f"\n- 还有 {len(parse_errors) - 20} 个文件解析失败"
    raise RuntimeError(f"调用图扫描解析源码失败，不能产出可信调用图：\n{details}")


def _print_summary(summary: Dict[str, Any], risk: List[Dict[str, Any]]) -> None:
    print("=== 函数级调用图 + 数据流提取完成 ===")
    print(
        f"函数数: {summary['total_functions']}  边: {summary['total_edges']} "
        f"(确信 {summary['confident_edges']} / 模糊 {summary['ambiguous_edges']})"
    )
    print(f"动态未消解: {summary['dynamic_unresolved_sites']}  数据流命中: {summary['dataflow_flagged_funcs']}")
    print(f"图指标: {summary['graph_metrics']}")
    print("cycle_count 口径: 确信边上的长度 2..8 简单循环记录数，最多 200 条；不是强连通组数量。")
    print(f"高风险数据流路径: {summary['risk_dataflow_count']}  高扇入咽喉: {summary['high_fan_in_count']}")
    print("\n--- 高风险数据流路径 Top 15 ---")
    for row in risk[:15]:
        print(f"  fan_in={row['fan_in']:3d}  {row['rel']}:{row['line']}  {'/'.join(row['co_occurrence'])}")
    print("\n产物目录: " + OUT_DIR)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="函数级调用图提取器（支持 light 轻量 AST 档与 deep 重型 SCIP/Runtime 档）")
    parser.add_argument(
        "--mode",
        choices=("light", "deep"),
        default=os.environ.get("CALLGRAPH_MODE", "light"),
        help="light=默认轻量 AST 类型消解(秒级)；deep=额外融合 SCIP 索引与运行时调用边",
    )
    parser.add_argument("--scip-index", default=None, help="deep 模式下指定 index.scip 路径")
    parser.add_argument("--runtime-edges", default=None, help="deep 模式下指定 runtime_edges.json 路径")
    parser.add_argument("--repo-root", default=None, help="指定待扫描仓库根目录（默认当前仓库根）")
    args = parser.parse_args(argv)
    if args.repo_root:
        global REPO_ROOT
        REPO_ROOT = os.path.abspath(args.repo_root)

    os.makedirs(OUT_DIR, exist_ok=True)
    all_funcs, name_to_quals, file_funcs, file_imports, trees, parse_errors = _index_codebase()
    _raise_parse_errors(parse_errors)
    edges, dynamic_unresolved, dataflow_nodes = _resolve_edges(all_funcs, name_to_quals, file_funcs, file_imports, trees)
    if args.mode == "deep":
        scip_path = args.scip_index or os.path.join(REPO_ROOT, ".checkup", "scip", "index.scip")
        rt_path = args.runtime_edges or os.path.join(REPO_ROOT, ".checkup", "callgraph_runtime", "runtime_edges.json")
        edges, deep_stats = _tresolver.apply_deep_resolution(
            REPO_ROOT,
            edges,
            all_funcs,
            scip_index_path=scip_path,
            runtime_edges_path=rt_path,
        )
        print(f"[deep 模式融合] {deep_stats}")
    fan_in, fan_out, ambiguous_fan_in, ambiguous_fan_out = _fan_counts(edges)
    summary, risk = _write_outputs(
        all_funcs,
        edges,
        dynamic_unresolved,
        dataflow_nodes,
        fan_in,
        fan_out,
        ambiguous_fan_in,
        ambiguous_fan_out,
        parse_errors,
    )
    _print_summary(summary, risk)


if __name__ == "__main__":
    main()
