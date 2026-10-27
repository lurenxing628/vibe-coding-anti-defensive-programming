#!/usr/bin/env python3
"""静态调用图 × 运行时调用边 对账器（动态交叉验证）。

读取静态图 (edges.json / functions.json / islands.json) 和 runtime_callgraph_collect.py 录下的运行时边，
回答三件事：
  1) 模糊边（ambiguous）里运行时真跑到了的 -> 可从“问号”升级为“确信实线”；
  2) 运行时有、静态完全没有的边 -> 静态漏掉的动态派发（getattr / 动态回调等），补齐静态盲区；
  3) 静态判“孤岛”（零进零出）里运行时其实跑到了的 -> 排除假阳性，从未跑到的才是高置信度死代码候选。

用法:
    python3 tools/callgraph/callgraph_crossval.py
环境变量:
    CHECKUP_CALLGRAPH          静态图产物目录（默认 .checkup/callgraph）
    CHECKUP_CALLGRAPH_RUNTIME  运行时产物目录（默认 .checkup/callgraph_runtime）
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATIC_DIR = os.path.abspath(os.environ.get("CHECKUP_CALLGRAPH") or os.path.join(REPO_ROOT, ".checkup", "callgraph"))
RUNTIME_DIR = os.path.abspath(os.environ.get("CHECKUP_CALLGRAPH_RUNTIME") or os.path.join(REPO_ROOT, ".checkup", "callgraph_runtime"))

Pair = Tuple[str, str]


def _load(directory: str, name: str) -> Any:
    with open(os.path.join(directory, name), encoding="utf-8") as fh:
        return json.load(fh)


def _static_pairs(edges: List[Dict[str, Any]]) -> Tuple[Set[Pair], Set[Pair]]:
    confident: Set[Pair] = set()
    ambiguous: Set[Pair] = set()
    for edge in edges:
        pair = (edge["from"], edge["to"])
        (ambiguous if edge["ambiguous"] else confident).add(pair)
    return confident, ambiguous


def _samples(pairs: Set[Pair], limit: int = 12) -> List[Dict[str, str]]:
    return [{"from": src, "to": dst} for src, dst in sorted(pairs)[:limit]]


def _write_report(report: Dict[str, Any]) -> None:
    path = os.path.join(RUNTIME_DIR, "crossval_report.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def main() -> None:
    static_edges = _load(STATIC_DIR, "edges.json")
    islands = set(_load(STATIC_DIR, "islands.json"))
    runtime_edges = _load(RUNTIME_DIR, "runtime_edges.json")
    reachable = set(_load(RUNTIME_DIR, "reachable_nodes.json"))

    confident, ambiguous = _static_pairs(static_edges)
    runtime_pairs: Set[Pair] = {(edge["from"], edge["to"]) for edge in runtime_edges}

    confirmed_ambiguous = runtime_pairs & ambiguous
    matched_confident = runtime_pairs & confident
    static_missed = runtime_pairs - ambiguous - confident

    islands_reached = islands & reachable
    islands_never = islands - reachable

    total_edges = len(confident) + len(ambiguous)
    before_ratio = len(confident) / total_edges if total_edges else 0.0
    after_ratio = (len(confident) + len(confirmed_ambiguous)) / total_edges if total_edges else 0.0

    report = {
        "static": {"confident": len(confident), "ambiguous": len(ambiguous), "total": total_edges},
        "runtime": {"edges": len(runtime_pairs), "reachable_nodes": len(reachable)},
        "ambiguous_confirmed": {
            "count": len(confirmed_ambiguous),
            "pct_of_ambiguous": round(len(confirmed_ambiguous) / len(ambiguous), 4) if ambiguous else 0.0,
            "samples": _samples(confirmed_ambiguous),
        },
        "confident_corroborated": len(matched_confident),
        "static_missed": {"count": len(static_missed), "samples": _samples(static_missed)},
        "islands": {
            "total": len(islands),
            "reached_at_runtime_false_alarm": len(islands_reached),
            "never_reached_deadcode_candidate": len(islands_never),
        },
        "confident_ratio_before": round(before_ratio, 4),
        "confident_ratio_after_promotion": round(after_ratio, 4),
        "note": "运行时仅覆盖本次采集子集，确认数为下界；死代码嫌疑须结合全仓检索核验。",
    }
    _write_report(report)

    print("=== 静态 × 运行时 对账 ===")
    print(f"静态边: 确信 {len(confident)} / 模糊 {len(ambiguous)} (合计 {total_edges})")
    print(f"运行时边: {len(runtime_pairs)}  可达函数: {len(reachable)}")
    print("")
    print(f"[1] 模糊边被运行时确认(问号->实线): {len(confirmed_ambiguous)} "
          f"({report['ambiguous_confirmed']['pct_of_ambiguous']:.1%} 的模糊边)")
    print(f"    确信占比: {before_ratio:.1%} -> {after_ratio:.1%}(本子集提升后)")
    print(f"[2] 静态漏掉、运行时抓到的边(动态派发): {len(static_missed)}")
    print(f"[3] 孤岛 {len(islands)}: 运行时跑到的假警报 {len(islands_reached)} / 从没跑到的死代码嫌疑 {len(islands_never)}")
    print(f"    (确信边被运行时佐证 {len(matched_confident)} 条,作健全性检查)")
    print("\n报告: " + os.path.join(RUNTIME_DIR, "crossval_report.json"))


if __name__ == "__main__":
    main()
