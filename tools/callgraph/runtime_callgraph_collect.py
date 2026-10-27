#!/usr/bin/env python3
"""运行时调用边采集器（动态交叉验证 · sys.setprofile 录像）。

用 sys.setprofile / threading.setprofile 在测试运行时录下真实发生过的 caller -> callee 边，
按静态图同款全限定名 (rel::Class.method) 消解，只保留第一方函数之间的边。
产物交给 callgraph_crossval.py 和静态图对账：
  - 静态标“模糊（ambiguous）”的边里，运行时真跑到的可升级为确信实线；
  - 运行时有而静态完全没有的边，即为静态分析漏掉的动态派发（getattr / 回调 / 鸭子类型等）。

只读不改业务代码。用法:
    python3 tools/callgraph/runtime_callgraph_collect.py [pytest_args...]
环境变量:
    CHECKUP_CALLGRAPH          静态图产物目录（默认 .checkup/callgraph）
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
STATIC_DIR = os.path.abspath(os.environ.get("CHECKUP_CALLGRAPH") or os.path.join(REPO_ROOT, ".checkup", "callgraph"))
OUT_DIR = os.path.abspath(os.environ.get("CHECKUP_CALLGRAPH_RUNTIME") or os.path.join(REPO_ROOT, ".checkup", "callgraph_runtime"))

FIRST_PARTY_ROOTS = ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop")
_FP_PREFIXES = tuple(os.path.join(REPO_ROOT, root) + os.sep for root in FIRST_PARTY_ROOTS)

_fp_cache: Dict[CodeType, bool] = {}
_raw_edges: Set[Tuple[CodeType, CodeType]] = set()


def _is_first_party(code: CodeType) -> bool:
    cached = _fp_cache.get(code)
    if cached is None:
        cached = code.co_filename.startswith(_FP_PREFIXES)
        _fp_cache[code] = cached
    return cached


def _profile(frame: Any, event: str, arg: Any) -> None:
    """只记 Python 函数进入事件；caller=上一帧 code，callee=本帧 code，两端都得是第一方。"""
    if event != "call":
        return
    back = frame.f_back
    if back is None:
        return
    callee = frame.f_code
    if not _is_first_party(callee):
        return
    caller = back.f_code
    if _is_first_party(caller):
        _raw_edges.add((caller, callee))


def _rel(filename: str) -> str:
    return os.path.relpath(filename, REPO_ROOT).replace("\\", "/")


def _load_static_index() -> Dict[Tuple[str, str], List[Tuple[str, int, int]]]:
    with open(os.path.join(STATIC_DIR, "functions.json"), encoding="utf-8") as fh:
        funcs = json.load(fh)
    index: Dict[Tuple[str, str], List[Tuple[str, int, int]]] = defaultdict(list)
    for qual, info in funcs.items():
        index[(info["rel"], info["name"])].append((qual, int(info["line"]), int(info["end"])))
    return index


def _resolve(code: CodeType, index: Dict[Tuple[str, str], List[Tuple[str, int, int]]]) -> Optional[str]:
    candidates = index.get((_rel(code.co_filename), code.co_name))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    line = code.co_firstlineno
    contained = [qual for qual, lo, hi in candidates if lo <= line <= hi]
    if len(contained) == 1:
        return contained[0]
    return min(candidates, key=lambda item: abs(item[1] - line))[0]


def _resolve_edges(index: Dict[Tuple[str, str], List[Tuple[str, int, int]]]) -> Tuple[Set[Tuple[str, str]], Set[str], int]:
    edges: Set[Tuple[str, str]] = set()
    nodes: Set[str] = set()
    unresolved = 0
    for caller, callee in _raw_edges:
        src = _resolve(caller, index)
        dst = _resolve(callee, index)
        if src is None or dst is None:
            unresolved += 1
            continue
        edges.add((src, dst))
        nodes.add(src)
        nodes.add(dst)
    return edges, nodes, unresolved


def _run_pytest(argv: List[str]) -> int:
    import pytest

    sys.setprofile(_profile)
    threading.setprofile(_profile)
    try:
        return int(pytest.main(["-o", "addopts=", "-p", "no:cacheprovider", "-q", *argv]))
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
    index = _load_static_index()
    exit_code = _run_pytest(argv)
    edges, nodes, unresolved = _resolve_edges(index)
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
