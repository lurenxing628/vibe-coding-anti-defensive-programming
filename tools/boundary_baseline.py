"""边界棘轮基线：各静态治理规则（over_defense / private_import / dead_path_literal 等）共用的基线读写与比较。

口径：
- 条目键为 (path, kind)，值为 count。
- 当前有、基线无 → new；基线有、当前无 → stale（请从基线移除）；同键 count 变大 → increased。
- 三者任一非空即门禁失败；count 变小允许，但提示刷新。
- 刷新只能通过扫描器 --refresh；默认拒绝总命中数增长，除非显式 --allow-growth。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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
    def clean(self) -> bool:
        return not self.baseline_missing and not self.has_debt


def default_baseline_path(repo_root: str, rule: str) -> str:
    base_dir = os.environ.get("QUALITY_BASELINE_DIR") or os.path.join(repo_root, ".checkup")
    return os.path.join(base_dir, f"{rule}_baseline.json")


def entries_to_counts(entries: Iterable[Dict[str, object]]) -> Dict[EntryKey, int]:
    counts: Dict[EntryKey, int] = {}
    for entry in entries:
        path = str(entry.get("path") or "").replace("\\", "/").strip()
        kind = str(entry.get("kind") or "").strip()
        if not path or not kind:
            raise BoundaryBaselineError("条目缺少 path/kind，不能生成可信基线")
        counts[(path, kind)] = counts.get((path, kind), 0) + int(entry.get("count") or 0)
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
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise BoundaryBaselineError(f"基线 {path} 的 schema_version 不受支持，请人工核对后受控重建")
    if str(payload.get("rule") or "") != expected_rule:
        raise BoundaryBaselineError(f"基线 {path} 的 rule={payload.get('rule')!r} 与期望 {expected_rule!r} 不符")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise BoundaryBaselineError(f"基线 {path} 的 entries 必须是数组")
    return entries_to_counts(entries)


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
            {"path": key[0], "kind": key[1], "count": int(value)}
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
    return sum(int(value) for value in counts.values())
