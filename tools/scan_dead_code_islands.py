#!/usr/bin/env python3
"""死代码孤岛防回潮扫描器。

复用静态调用图工具 tools/callgraph/callgraph_extract.py:把它的产物输出
经 CHECKUP_CALLGRAPH 重定向到 OS 临时目录(绝不弄脏已提交目录、
不破坏净树证明),读临时目录里的 functions/edges/islands,再用真实使用图过滤误报。

quick 模式只用 AST 使用图,适合 pre-push。
precise 模式要求新鲜 SCIP 索引;索引缺失/过期/工作区脏时直接失败,不回退。

--warn-only 只把"发现新增疑似死代码"降成提示;工具错误仍然非零退出。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Set, cast

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.dead_code_usage.ast_usage import collect_ast_usage
from tools.dead_code_usage.model import UsageEvidence, records_from_functions

CALLGRAPH_EXTRACT = os.path.join(REPO_ROOT, "tools", "callgraph", "callgraph_extract.py")
BASELINE_PATH = os.path.join(REPO_ROOT, ".checkup", "dead_code_islands_baseline.json")
BASELINE_REL = os.path.relpath(BASELINE_PATH, REPO_ROOT)


def _compute_callgraph_snapshot() -> Dict[str, object]:
    """跑 callgraph_extract 到临时目录,返回当前函数/边/候选孤岛。"""
    tmp_dir = tempfile.mkdtemp(prefix="dead_code_islands_")
    try:
        env = dict(os.environ)
        env["CHECKUP_CALLGRAPH"] = tmp_dir
        proc = subprocess.run(
            [sys.executable, CALLGRAPH_EXTRACT],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        paths = {
            "functions": os.path.join(tmp_dir, "functions.json"),
            "edges": os.path.join(tmp_dir, "edges.json"),
            "islands": os.path.join(tmp_dir, "islands.json"),
        }
        if proc.returncode != 0 or not all(os.path.isfile(path) for path in paths.values()):
            tail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise RuntimeError("callgraph_extract 未产出完整调用图：" + tail)
        with open(paths["functions"], encoding="utf-8") as handle:
            functions = json.load(handle)
        with open(paths["edges"], encoding="utf-8") as handle:
            edges = json.load(handle)
        with open(paths["islands"], encoding="utf-8") as handle:
            islands = json.load(handle)
        return {"functions": functions, "edges": edges, "islands": [str(item) for item in islands]}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _load_baseline() -> Optional[Set[str]]:
    """读基线孤岛集;无基线返回 None(首次需 --refresh 建立)。"""
    if not os.path.isfile(BASELINE_PATH):
        return None
    with open(BASELINE_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    return {str(item) for item in (data.get("islands") or [])}


def _write_baseline(islands: Sequence[str], mode: str) -> None:
    payload = {
        "note": (
            "死代码疑似项基线。候选来自调用图孤岛,再经过真实使用图过滤。"
            f"本基线按 {mode} 口径生成；受控更新："
            f"python tools/scan_dead_code_islands.py --mode {mode} --refresh。"
        ),
        "schema_version": 2,
        "mode": mode,
        "count": len(islands),
        "islands": sorted(islands),
    }
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    with open(BASELINE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def _analyze(mode: str, index_path: Optional[str]) -> Dict[str, object]:
    snapshot = _compute_callgraph_snapshot()
    functions = snapshot["functions"]  # type: ignore[assignment]
    records = records_from_functions(functions)  # type: ignore[arg-type]
    candidates = set(snapshot["islands"])  # type: ignore[arg-type]
    ast_evidence = collect_ast_usage(REPO_ROOT, records)
    scip_evidence = {}  # type: Dict[str, List[UsageEvidence]]
    if mode == "precise":
        scip_evidence = _collect_scip_usage(REPO_ROOT, records, index_path=index_path)
    live = set()
    evidence = {}  # type: Dict[str, List[UsageEvidence]]
    for qual in candidates:
        items = list(ast_evidence.get(qual, [])) + list(scip_evidence.get(qual, []))
        if items:
            live.add(qual)
            evidence[qual] = items
    suspect_dead = sorted(candidates - live)
    return {
        "mode": mode,
        "candidate_count": len(candidates),
        "live_count": len(live),
        "suspect_dead": suspect_dead,
        "evidence": evidence,
    }


def _collect_scip_usage(repo_root, records, index_path=None):
    from tools.dead_code_usage.scip_usage import collect_scip_usage

    return collect_scip_usage(repo_root, records, index_path=index_path)


def _print_summary(result: Dict[str, object]) -> None:
    suspect_dead = cast(List[str], result["suspect_dead"])
    print(
        "[dead-code-islands] "
        f"precision={result['mode']} candidates={result['candidate_count']} "
        f"explained={result['live_count']} suspect_dead={len(suspect_dead)}",
        flush=True,
    )
    if result["mode"] == "quick":
        print("[dead-code-islands] quick 模式未使用 SCIP;如需精确确认,运行 --mode precise。", flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="死代码孤岛防回潮扫描器")
    parser.add_argument("--mode", choices=("quick", "precise"), default="quick", help="quick=AST 使用图;precise=要求新鲜 SCIP")
    parser.add_argument("--index-path", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--warn-only", action="store_true", help="发现新增疑似死代码时只提示不阻断")
    parser.add_argument("--refresh", action="store_true", help="把基线重写为当前疑似死代码集")
    parser.add_argument(
        "--quiet-when-clean",
        action="store_true",
        help="无新增孤岛时完全不输出（pre-push 钩子用：平时安静，有新孤岛才大声报）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    scip_error_type = None
    if args.mode == "precise":
        from tools.dead_code_usage.scip_usage import ScipUsageError

        scip_error_type = ScipUsageError

    try:
        result = _analyze(args.mode, args.index_path)
    except Exception as exc:
        if scip_error_type is not None and isinstance(exc, scip_error_type):
            print(f"[dead-code-islands] 精确模式不可用：{exc.message}", flush=True)
            for hint in exc.hints:
                print(f"  - {hint}", flush=True)
            return 2
        print(f"[dead-code-islands] 扫描失败：{exc}", flush=True)
        return 2
    current = list(result["suspect_dead"])  # type: ignore[arg-type]

    if args.refresh:
        _print_summary(result)
        _write_baseline(current, args.mode)
        print(f"[dead-code-islands] 基线已刷新：{len(current)} 个疑似死代码 -> {BASELINE_REL}", flush=True)
        return 0

    baseline = _load_baseline()
    if baseline is None:
        print(
            "[dead-code-islands] 未建立基线（先跑 `python tools/scan_dead_code_islands.py --refresh`）；本次跳过对比。",
            flush=True,
        )
        return 0

    current_set = set(current)
    new_islands = sorted(current_set - baseline)
    gone_islands = sorted(baseline - current_set)

    if args.quiet_when_clean and not new_islands:
        return 0  # 平时安静：无新增孤岛就不吭声（pre-push 钩子高频跑用）

    _print_summary(result)

    if new_islands:
        print(
            f"⚠️ [dead-code-islands] 检出 {len(new_islands)} 个新增疑似死代码：",
            flush=True,
        )
        for qual in new_islands:
            print(f"    + {qual}", flush=True)
        print(
            "  处理：确认是真死代码就删；若是当前设计接受的未用入口，"
            f"跑 `python tools/scan_dead_code_islands.py --mode {args.mode} --refresh` 受控刷新基线。",
            flush=True,
        )
    else:
        print(f"[dead-code-islands] 无新增疑似死代码（基线 {len(baseline)} 个）。", flush=True)

    if gone_islands:
        print(
            f"[dead-code-islands] {len(gone_islands)} 个基线疑似项已消失（已清理/已接线）；"
            f"可 --mode {args.mode} --refresh 收敛基线。",
            flush=True,
        )

    if new_islands and not args.warn_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
