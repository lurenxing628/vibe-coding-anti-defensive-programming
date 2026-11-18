"""棘轮基线：SQL 边界、仓储策略、私有导入、失效路径字面量、反过度防御、死代码孤岛扫描器共用的基线读写与比较。

棘轮口径：
- 条目键为 (path, kind)，值为 count。
- 当前有、基线无 → new；基线有、当前无 → stale（请从基线移除）；同键 count 变大 → increased。
- 三者任一非空即门禁失败；count 变小允许，但提示刷新。
- 刷新只能通过扫描器 --refresh：默认读旧基线，拒绝任何新增或计数增加的条目；
  --allow-growth 表示人工核对后按当前扫描整份重写，不读旧基线（与循环依赖扫描器 --update-baseline --allow-growth 同一语义）。
- 各扫描器的棘轮命令行参数统一由 add_ratchet_arguments 定义，evaluate_ratchet_rule 只读这些字段。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from tools.repo_paths import resolve_repo_root

SCHEMA_VERSION = 1
EntryKey = Tuple[str, str]


class BoundaryBaselineError(ValueError):
    """基线存在但无法可信读取。"""


@dataclass
class BaselineComparison:
    baseline_missing: bool
    new: Dict[EntryKey, int] = field(default_factory=dict)
    stale: Dict[EntryKey, int] = field(default_factory=dict)
    increased: Dict[EntryKey, Tuple[int, int]] = field(default_factory=dict)
    decreased: Dict[EntryKey, Tuple[int, int]] = field(default_factory=dict)

    @property
    def has_debt(self) -> bool:
        return bool(self.new or self.stale or self.increased)

    @property
    def has_growth(self) -> bool:
        return bool(self.new or self.increased)

    @property
    def clean(self) -> bool:
        return not self.baseline_missing and not self.has_debt


def default_baseline_path(repo_root: str, rule: str) -> str:
    base_dir = os.environ.get("QUALITY_BASELINE_DIR") or os.path.join(repo_root, ".checkup")
    return os.path.join(base_dir, f"{rule}_baseline.json")


def entries_to_counts(entries: Iterable[Dict[str, Any]]) -> Dict[EntryKey, int]:
    counts: Dict[EntryKey, int] = {}
    for entry in entries:
        key = (entry["path"], entry["kind"])
        counts[key] = counts.get(key, 0) + entry["count"]
    return counts


def load_baseline(path: str, *, expected_rule: str) -> Optional[Dict[EntryKey, int]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise BoundaryBaselineError(f"基线 {path} 无法读取：{exc}") from exc
    if not isinstance(payload, dict):
        raise BoundaryBaselineError(f"基线 {path} 不是对象")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise BoundaryBaselineError(f"基线 {path} 的 schema_version 不受支持，请人工核对后受控重建")
    if payload.get("rule") != expected_rule:
        raise BoundaryBaselineError(f"基线 {path} 的 rule={payload.get('rule')!r} 与期望 {expected_rule!r} 不符")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise BoundaryBaselineError(f"基线 {path} 的 entries 必须是数组")
    counts: Dict[EntryKey, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise BoundaryBaselineError(f"基线 {path} 的条目必须是对象")
        raw_path = entry.get("path")
        raw_kind = entry.get("kind")
        if not isinstance(raw_path, str) or not isinstance(raw_kind, str):
            raise BoundaryBaselineError(f"基线 {path} 的条目缺少 path/kind，不能生成可信基线")
        entry_path = raw_path.replace("\\", "/").strip()
        kind = raw_kind.strip()
        if not entry_path or not kind:
            raise BoundaryBaselineError(f"基线 {path} 的条目缺少 path/kind，不能生成可信基线")
        count = entry.get("count")
        if type(count) is not int or count <= 0:
            raise BoundaryBaselineError(f"基线 {path} 的条目 count 必须为正整数：{count!r}")
        key = (entry_path, kind)
        if key in counts:
            raise BoundaryBaselineError(f"基线 {path} 存在重复条目：{entry_path} kind={kind}")
        counts[key] = count
    return counts


def write_baseline(
    path: str,
    *,
    rule: str,
    scan_roots: Sequence[str],
    note: str,
    counts: Dict[EntryKey, int],
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "rule": rule,
        "scan_roots": list(scan_roots),
        "note": note,
        "entries": [
            {"path": key[0], "kind": key[1], "count": value}
            for key, value in sorted(counts.items())
        ],
    }
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".baseline_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_path, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def compare_counts(current: Dict[EntryKey, int], baseline: Optional[Dict[EntryKey, int]]) -> BaselineComparison:
    if baseline is None:
        return BaselineComparison(baseline_missing=True)
    comparison = BaselineComparison(baseline_missing=False)
    for key, value in current.items():
        if key not in baseline:
            comparison.new[key] = value
        elif value > baseline[key]:
            comparison.increased[key] = (baseline[key], value)
        elif value < baseline[key]:
            comparison.decreased[key] = (baseline[key], value)
    for key, value in baseline.items():
        if key not in current:
            comparison.stale[key] = value
    return comparison


def render_comparison(rule: str, comparison: BaselineComparison) -> List[str]:
    lines: List[str] = []
    prefix = f"[{rule}]"
    if comparison.baseline_missing:
        lines.append(f"{prefix} 基线缺失；请先查看扫描结果，确认后执行 --refresh 建立基线")
        return lines
    for key, value in sorted(comparison.new.items()):
        lines.append(f"{prefix} 新增债务：{key[0]} kind={key[1]} count={value}")
    for key, (before, after) in sorted(comparison.increased.items()):
        lines.append(f"{prefix} 债务增加：{key[0]} kind={key[1]} {before} -> {after}")
    for key, value in sorted(comparison.stale.items()):
        lines.append(f"{prefix} 基线条目已清理，请从基线移除（--refresh）：{key[0]} kind={key[1]} count={value}")
    for key, (before, after) in sorted(comparison.decreased.items()):
        lines.append(f"{prefix} 债务减少（建议 --refresh 收紧基线）：{key[0]} kind={key[1]} {before} -> {after}")
    if not lines:
        lines.append(f"{prefix} 相对基线无新增债务")
    return lines


def total_count(counts: Dict[EntryKey, int]) -> int:
    return sum(counts.values())


def refresh_growth_error(
    rule: str,
    current: Dict[EntryKey, int],
    previous: Optional[Dict[EntryKey, int]],
) -> Optional[str]:
    """基线刷新只允许保持或收敛存量；任何新增条目或单条目计数增长都拒绝（防删旧抵新）。"""
    if previous is None:
        return None
    comparison = compare_counts(current, previous)
    if not comparison.has_growth:
        return None
    details: List[str] = []
    for (path, kind), count in sorted(comparison.new.items()):
        details.append(f"  新增：{path} kind={kind} count={count}")
    for (path, kind), (before, after) in sorted(comparison.increased.items()):
        details.append(f"  增加：{path} kind={kind} {before} -> {after}")
    return (
        f"{rule} 刷新被拒绝：存在新增或计数增加的条目（总命中数 {total_count(previous)} -> {total_count(current)}）；"
        "请先消除新增债务，或人工核对后加 --allow-growth\n" + "\n".join(details)
    )


def add_ratchet_arguments(parser: argparse.ArgumentParser) -> None:
    """棘轮扫描器共用的命令行参数；帮助文字按 evaluate_ratchet_rule 的实际行为写。"""
    parser.add_argument(
        "--json",
        action="store_true",
        help="把扫描结果以 JSON 输出到 stdout，不再输出文字结果；退出码规则不变",
    )
    parser.add_argument(
        "--fail-on-new",
        action="store_true",
        help="与基线比较：出现新增条目、条目计数增加或基线条目已清理即退出码 1；基线缺失或无法读取退出码 2",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "按当前扫描重写基线：先读旧基线，出现新增条目或任一条目计数增加就拒绝写入并退出码 1"
            "（总命中数下降也不放行）；旧基线无法读取退出码 2，旧基线不存在时直接建立"
        ),
    )
    parser.add_argument(
        "--allow-growth",
        action="store_true",
        help="与 --refresh 合用：人工核对增长后按当前扫描整份重写基线，不读旧基线",
    )
    parser.add_argument(
        "--quiet-when-clean",
        action="store_true",
        help="与 --fail-on-new 合用：相对基线没有新增、增加或已清理的条目时不输出",
    )
    parser.add_argument("--repo-root", default=resolve_repo_root(), help=argparse.SUPPRESS)


def evaluate_ratchet_rule(
    args: argparse.Namespace,
    *,
    rule: str,
    result: Dict[str, Any],
    note: str,
    render_text: Callable[[Dict[str, Any]], str],
    error_fn: Callable[[str], None],
) -> Tuple[int, List[str]]:
    if not args.refresh and not args.fail_on_new:
        return 0, ([] if args.json else [render_text(result)])

    current = entries_to_counts(result["entries"])
    baseline_path = default_baseline_path(args.repo_root, rule)
    if args.refresh:
        if not args.allow_growth:
            try:
                previous = load_baseline(baseline_path, expected_rule=rule)
            except BoundaryBaselineError as exc:
                error_fn(str(exc))
                return 2, []
            growth_error = refresh_growth_error(rule, current, previous)
            if growth_error is not None:
                error_fn(growth_error)
                return 1, []
        write_baseline(
            baseline_path,
            rule=rule,
            scan_roots=result["scan_roots"],
            note=note,
            counts=current,
        )
        if args.json:
            return 0, []
        return 0, [
            f"[{rule}] 基线已写入 {os.path.relpath(baseline_path, args.repo_root)}："
            f"{len(current)} 条目 / {total_count(current)} 处"
        ]

    exit_code = 0
    comparison: Optional[BaselineComparison] = None
    try:
        baseline = load_baseline(baseline_path, expected_rule=rule)
    except BoundaryBaselineError as exc:
        error_fn(str(exc))
        exit_code = 2
    else:
        comparison = compare_counts(current, baseline)
        if comparison.baseline_missing:
            error_fn(f"{rule} 基线缺失：{baseline_path}；请先查看扫描结果并受控执行 --refresh")
            exit_code = 2
        elif comparison.has_debt:
            exit_code = 1
    if args.json or comparison is None or comparison.baseline_missing:
        return exit_code, []
    if comparison.clean and args.quiet_when_clean:
        return 0, []
    return exit_code, render_comparison(rule, comparison)


def run_ratchet_cli(
    args: argparse.Namespace,
    *,
    rule: str,
    result: Dict[str, Any],
    note: str,
    render_text: Callable[[Dict[str, Any]], str],
    error_fn: Callable[[str], None],
) -> int:
    exit_code, lines = evaluate_ratchet_rule(
        args,
        rule=rule,
        result=result,
        note=note,
        render_text=render_text,
        error_fn=error_fn,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for line in lines:
            print(line, flush=True)
    return exit_code
