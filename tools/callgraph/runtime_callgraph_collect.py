#!/usr/bin/env python3
"""运行时调用边采集器（动态交叉验证 · sys.setprofile 录像）。

用 sys.setprofile / threading.setprofile 在测试运行时录下真实发生过的 caller -> callee 边，
按静态图同款全限定名 (rel::Class.method) 消解，只保留第一方函数之间的边。
产物交给 callgraph_extract.py (--mode deep) 或 callgraph_crossval.py 对账：
  - 静态标“模糊（ambiguous）”的边里，运行时真跑到的直接升级为确信实线并剪除同调用点假候选；
  - 运行时有而静态完全没有的边，即为静态分析漏掉的动态派发（getattr / 回调 / 鸭子类型等）。

只读不改业务代码。用法:
    python3 tools/callgraph/runtime_callgraph_collect.py [pytest_args...]
环境变量:
    CHECKUP_CALLGRAPH          静态图产物目录（可选；未设置时直接实时索引当前源码）
    CHECKUP_CALLGRAPH_RUNTIME  运行时产物目录（默认 .checkup/callgraph_runtime）
"""
from __future__ import annotations

import json
import os
import sys
import threading
from collections import defaultdict
from types import CodeType
from typing import Any, Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATIC_DIR = os.environ.get("CHECKUP_CALLGRAPH")
OUT_DIR = os.path.abspath(
    os.environ.get("CHECKUP_CALLGRAPH_RUNTIME") or os.path.join(REPO_ROOT, ".checkup", "callgraph_runtime")
)

if HERE not in sys.path:
    sys.path.insert(0, HERE)

FIRST_PARTY_ROOTS = ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop")
_FP_PREFIXES = tuple(os.path.join(REPO_ROOT, root) + os.sep for root in FIRST_PARTY_ROOTS)
_COMP_NAMES = frozenset({"<listcomp>", "<dictcomp>", "<setcomp>", "<genexpr>"})

_fp_cache: Dict[CodeType, bool] = {}
_raw_edges: Set[Tuple[CodeType, CodeType]] = set()

NameIndex = Dict[Tuple[str, str], List[Tuple[str, int, int]]]
SpanIndex = Dict[str, List[Tuple[int, int, str]]]


def _is_first_party(code: CodeType) -> bool:
    cached = _fp_cache.get(code)
    if cached is None:
        fn = code.co_filename
        cached = fn.startswith(_FP_PREFIXES) or (
            os.path.dirname(fn) == REPO_ROOT and fn.endswith(".py")
        )
        _fp_cache[code] = cached
    return cached


def _profile(frame: Any, event: str, arg: Any) -> None:
    """只记 Python 函数进入事件；若上一帧是推导式 (<listcomp>/<genexpr>)，向上穿透到真实外层函数帧。"""
    if event != "call":
        return
    callee = frame.f_code
    if callee.co_name in _COMP_NAMES or not _is_first_party(callee):
        return
    back = frame.f_back
    while back is not None and back.f_code.co_name in _COMP_NAMES:
        back = back.f_back
    if back is None:
        return
    caller = back.f_code
    if _is_first_party(caller):
        _raw_edges.add((caller, callee))


def _rel(filename: str) -> str:
    return os.path.relpath(filename, REPO_ROOT).replace("\\", "/")


def _load_static_index() -> Tuple[NameIndex, SpanIndex]:
    index: NameIndex = defaultdict(list)
    spans: SpanIndex = defaultdict(list)
    static_funcs_json = os.path.join(STATIC_DIR, "functions.json") if STATIC_DIR else ""
    if static_funcs_json and os.path.isfile(static_funcs_json):
        with open(static_funcs_json, encoding="utf-8") as fh:
            funcs = json.load(fh)
    else:
        import callgraph_extract as _cg

        funcs, *_ = _cg._index_codebase()
    for qual, info in funcs.items():
        rel = str(info["rel"])
        lo, hi = int(info["line"]), int(info["end"])
        index[(rel, str(info["name"]))].append((qual, lo, hi))
        spans[rel].append((lo, hi, qual))
    return index, spans


def _resolve(code: CodeType, index: NameIndex, spans: SpanIndex) -> Optional[str]:
    rel = _rel(code.co_filename)
    line = code.co_firstlineno
    candidates = index.get((rel, code.co_name))
    if candidates:
        if len(candidates) == 1:
            return candidates[0][0]
        contained = [qual for qual, lo, hi in candidates if lo <= line <= hi]
        if len(contained) == 1:
            return contained[0]
        return min(candidates, key=lambda item: abs(item[1] - line))[0]
    file_spans = spans.get(rel)
    if file_spans:
        enclosing = [(hi - lo, qual) for lo, hi, qual in file_spans if lo <= line <= hi]
        if enclosing:
            return min(enclosing)[1]
    return None


def _resolve_edges(index: NameIndex, spans: SpanIndex) -> Tuple[Set[Tuple[str, str]], Set[str], int]:
    edges: Set[Tuple[str, str]] = set()
    nodes: Set[str] = set()
    unresolved = 0
    for caller, callee in _raw_edges:
        src = _resolve(caller, index, spans)
        dst = _resolve(callee, index, spans)
        if src is None or dst is None:
            unresolved += 1
            continue
        if src != dst:
            edges.add((src, dst))
        nodes.add(src)
        nodes.add(dst)
    return edges, nodes, unresolved


def _run_pytest(argv: List[str]) -> int:
    import pytest

    sys.setprofile(_profile)
    threading.setprofile(_profile)
    try:
        return int(pytest.main(["-o", "addopts=", "-p", "no:cacheprovider", "-q", "-m", "not perf", *argv]))
    finally:
        sys.setprofile(None)
        threading.setprofile(None)


def _write_json(name: str, obj: Any) -> None:
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def main() -> None:
    argv = sys.argv[1:] or ["tests"]
    os.makedirs(OUT_DIR, exist_ok=True)
    index, spans = _load_static_index()
    exit_code = _run_pytest(argv)
    edges, nodes, unresolved = _resolve_edges(index, spans)
    _write_json("runtime_edges.json", [{"from": src, "to": dst} for src, dst in sorted(edges)])
    _write_json("reachable_nodes.json", sorted(nodes))
    _write_json("collect_summary.json", {
        "pytest_exit_code": exit_code,
        "subset": argv,
        "raw_edge_pairs": len(_raw_edges),
        "resolved_edges": len(edges),
        "unresolved_edge_pairs": unresolved,
        "reachable_nodes": len(nodes),
    })
    print("\n=== 运行时调用边采集完成 ===")
    print(f"pytest 退出码: {exit_code}（非 0 仅表示有用例失败/跳过，不影响采边）")
    print(f"原始 code 对: {len(_raw_edges)}  消解出第一方边: {len(edges)}  未消解: {unresolved}")
    print(f"运行时可达函数: {len(nodes)}")
    print("产物目录: " + OUT_DIR)


if __name__ == "__main__":
    main()
