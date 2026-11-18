#!/usr/bin/env python3
"""死代码孤岛棘轮扫描器（dead_code_islands 规则）。

候选：用 tools/callgraph/callgraph_extract.py（固定 --mode light，产物写到 OS 临时目录，
不碰已提交的 latest/callgraph/）取函数与调用边，去掉测试角色（路径角色见 tools/path_roles.py）的函数
及其相连的边后，仍没有任何调用边的函数。

使用证据按来源分栏（``Reference.origin``）：只用生产与宿主工具代码求解就有引用的算在用，不报；
只有测试证据的记 test_only（测试文件里的引用，及测试传入的类型经生产代码形成的引用）；没有任何引用的记 suspect_dead。
条目 path 是函数限定名、kind 是上面两种之一、count 恒为 1；基线走共用棘轮，只减不增。

quick 模式只用 AST 使用图（tools/dead_code_usage 的公开引用索引），进日常门禁与完整门禁。
precise 模式另加新鲜 SCIP 索引的引用，只出报告、不读写基线；索引缺失、过期或工作区脏时在跑调用图之前
直接失败，不回退。

用法：
    python -m tools.scan_dead_code_islands [--mode quick|precise] [--json] [--fail-on-new]
        [--refresh [--allow-growth]] [--quiet-when-clean]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

if __name__ == "__main__":  # 按文件路径运行时让仓库根下的 tools 包可导入
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.boundary_baseline import add_ratchet_arguments, run_ratchet_cli
from tools.dead_code_usage.ast_usage import collect_ast_usage
from tools.dead_code_usage.model import CodeRoles, UsageEvidence, records_from_functions
from tools.dead_code_usage.scip_usage import ScipUsageError, collect_scip_usage, load_precise_index
from tools.path_roles import ROLES, TEST, iter_role_files, role_of, role_roots
from tools.repo_paths import resolve_repo_root

#: 调用图提取脚本属于本工具自带资产，按工具所属仓库根定位；被扫描仓库由 repo_root 指定，可以是别的目录。
CALLGRAPH_EXTRACT_REL = os.path.join("tools", "callgraph", "callgraph_extract.py")
RULE = "dead_code_islands"
KINDS = ("suspect_dead", "test_only")
BASELINE_NOTE = (
    "死代码疑似项棘轮基线：候选是去掉测试角色后调用图里没有任何边的函数；"
    "suspect_dead=没有任何引用，test_only=只有测试代码引用。条目只减不增，"
    "受控刷新：python -m tools.scan_dead_code_islands --mode quick --refresh。"
)


class CallgraphError(RuntimeError):
    """调用图子进程失败，没有可信的函数与调用边。"""


def _callgraph(repo_root: str) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    out_dir = tempfile.mkdtemp(prefix="dead_code_islands_")
    try:
        env = dict(os.environ)
        env["CHECKUP_CALLGRAPH"] = out_dir
        extractor = os.path.join(resolve_repo_root(), CALLGRAPH_EXTRACT_REL)
        proc = subprocess.run(
            [sys.executable, extractor, "--mode", "light", "--repo-root", repo_root],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-600:]
            raise CallgraphError("callgraph_extract 未产出完整调用图：" + tail)
        with open(os.path.join(out_dir, "functions.json"), encoding="utf-8") as handle:
            functions = json.load(handle)
        with open(os.path.join(out_dir, "edges.json"), encoding="utf-8") as handle:
            edges = json.load(handle)
        return functions, edges
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _candidates(functions: Dict[str, Dict[str, Any]], edges: Iterable[Dict[str, Any]]) -> List[str]:
    """非测试函数里，和其他非测试函数之间没有任何调用边（含模糊边）的函数。"""
    non_test = {qual for qual, info in functions.items() if role_of(info["rel"]) != TEST}
    linked: Set[str] = set()
    for edge in edges:
        if edge["from"] in non_test and edge["to"] in non_test:
            linked.update((edge["from"], edge["to"]))
    return sorted(non_test - linked)


def _by_origin(items: Iterable[UsageEvidence]) -> Dict[str, List[UsageEvidence]]:
    grouped: Dict[str, List[UsageEvidence]] = defaultdict(list)
    for item in items:
        grouped[item.origin].append(item)
    return grouped


def scan(repo_root: str, mode: str = "quick", index_path: Optional[str] = None) -> Dict[str, Any]:
    # 精确模式先做 SCIP 前置检查并读索引：不满足时在跑调用图之前就失败。
    scip_docs = load_precise_index(repo_root, index_path=index_path) if mode == "precise" else None
    functions, edges = _callgraph(repo_root)
    records = records_from_functions(functions)
    candidates = _candidates(functions, edges)
    roles = CodeRoles(role_of, TEST)
    scip_evidence = collect_scip_usage(repo_root, scip_docs, records, roles) if scip_docs is not None else {}
    evidence = collect_ast_usage(repo_root, records, iter_role_files(repo_root, ROLES, (".py",)), roles)
    for target, items in scip_evidence.items():
        evidence.setdefault(target, []).extend(items)
    entries: List[Dict[str, Any]] = []
    live_count = 0
    for qual in candidates:
        by_origin = _by_origin(evidence.get(qual, []))
        if set(by_origin) - {TEST}:
            live_count += 1
            continue
        if by_origin:
            refs = sorted({f"{item.source_rel}:{item.line}" for item in by_origin[TEST]})
            entries.append({"path": qual, "kind": "test_only", "count": 1, "test_refs": refs})
        else:
            entries.append({"path": qual, "kind": "suspect_dead", "count": 1})
    kind_totals = {kind: 0 for kind in KINDS}
    for entry in entries:
        kind_totals[entry["kind"]] += 1
    return {
        "rule": RULE,
        "mode": mode,
        "scan_roots": list(role_roots(ROLES)),
        "candidate_count": len(candidates),
        "live_count": live_count,
        "kind_totals": kind_totals,
        "total": len(entries),
        "entries": entries,
    }


def render_text(result: Dict[str, Any]) -> str:
    totals = result["kind_totals"]
    lines = [
        f"[{RULE}] mode={result['mode']} 候选 {result['candidate_count']} 个，有生产或工具引用 {result['live_count']} 个，"
        f"疑似死代码 {totals['suspect_dead']} 个，只被测试引用 {totals['test_only']} 个"
    ]
    lines.extend(f"  {entry['kind']:12s} {entry['path']}" for entry in result["entries"])
    if result["mode"] == "quick":
        lines.append(f"[{RULE}] quick 模式未使用 SCIP；需要精确确认时运行 --mode precise（只出报告）。")
    return "\n".join(lines)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="死代码孤岛棘轮扫描器")
    parser.add_argument(
        "--mode",
        choices=("quick", "precise"),
        default="quick",
        help="quick=AST 使用图（门禁口径）；precise=另加新鲜 SCIP 引用，只出报告",
    )
    add_ratchet_arguments(parser)
    parser.add_argument("--index-path", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.mode == "precise" and (args.fail_on_new or args.refresh):
        parser.error("precise 模式只出报告，不读写基线；比较与刷新用 --mode quick")
    return args


def _error(message: str) -> None:
    print(f"[{RULE}] {message}", file=sys.stderr, flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        result = scan(args.repo_root, args.mode, args.index_path)
    except ScipUsageError as exc:
        _error(f"精确模式不可用：{exc.message}")
        for hint in exc.hints:
            _error(f"  - {hint}")
        return 2
    except (CallgraphError, SyntaxError, UnicodeDecodeError) as exc:
        _error(f"扫描失败，门禁已阻断：{exc}")
        return 2
    return run_ratchet_cli(
        args,
        rule=RULE,
        result=result,
        note=BASELINE_NOTE,
        render_text=render_text,
        error_fn=_error,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
