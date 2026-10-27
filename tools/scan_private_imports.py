"""私有符号跨模块导入扫描器（private_import 棘轮规则）。

口径：
- 扫描生产源码目录下 `from <模块> import _name`（_name 以单下划线开头、非 dunder）。
- 豁免：目标模块最后一段本身以 `_` 开头（私有模块）、`_frozen_import_anchor`、`from __future__`。
- 每个文件按 kind=private_symbol 计数；基线只减不增。

用法：
    python -m tools.scan_private_imports [--json] [--fail-on-new] [--refresh [--allow-growth]] [--quiet-when-clean]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

from tools.boundary_baseline import (
    BoundaryBaselineError,
    compare_counts,
    default_baseline_path,
    entries_to_counts,
    load_baseline,
    render_comparison,
    total_count,
    write_baseline,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
RULE = "private_import"
SCAN_ROOTS = ("core", "web", "data")
BASELINE_NOTE = "禁跨模块导入私有符号棘轮基线：拆文件必须给共享部分起公开名字；条目只减不增，刷新走 --refresh。"


def _iter_python_files(repo_root: str, roots: Sequence[str]) -> List[str]:
    files: List[str] = []
    for root in roots:
        base = os.path.join(repo_root, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    files.append(os.path.relpath(os.path.join(dirpath, filename), repo_root).replace("\\", "/"))
    return sorted(set(files))


def _is_private_symbol(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def scan_private_imports_file(rel_path: str, source: str) -> List[Dict[str, object]]:
    tree = ast.parse(source, filename=rel_path)
    hits: List[Dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module == "__future__":
            continue
        last_segment = module.rsplit(".", 1)[-1] if module else ""
        if last_segment.startswith("_"):
            continue
        for alias in node.names:
            if alias.name == "_frozen_import_anchor":
                continue
            if _is_private_symbol(alias.name):
                hits.append({"kind": "private_symbol", "line": node.lineno, "symbol": alias.name, "module": module})
    return hits


def scan(repo_root: str = REPO_ROOT) -> Dict[str, object]:
    entries: List[Dict[str, object]] = []
    files = _iter_python_files(repo_root, SCAN_ROOTS)
    for rel_path in files:
        with open(os.path.join(repo_root, rel_path), encoding="utf-8") as handle:
            hits = scan_private_imports_file(rel_path, handle.read())
        if hits:
            entries.append(
                {
                    "path": rel_path,
                    "kind": "private_symbol",
                    "count": len(hits),
                    "lines": sorted(int(hit["line"]) for hit in hits),
                    "symbols": sorted({str(hit["symbol"]) for hit in hits}),
                }
            )
    return {"rule": RULE, "scan_roots": list(SCAN_ROOTS), "file_count": len(files), "entries": entries,
            "total": sum(int(entry["count"]) for entry in entries)}


def render_text(result: Dict[str, object]) -> str:
    entries = list(result.get("entries") or [])
    lines = [f"[{RULE}] 扫描 {result['file_count']} 个文件，跨模块私有导入 {result['total']} 处，涉及 {len(entries)} 个文件"]
    for entry in sorted(entries, key=lambda item: (-int(item["count"]), str(item["path"])))[:15]:
        lines.append(f"  {entry['count']:3d}  {entry['path']}  ({', '.join(entry['symbols'][:5])})")
    return "\n".join(lines)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="私有符号跨模块导入棘轮扫描器")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-new", action="store_true", help="相对基线有新增/增加/失效条目即退出码 1；基线异常退出码 2")
    parser.add_argument("--refresh", action="store_true", help="按当前扫描结果重写基线（默认拒绝总命中数增长）")
    parser.add_argument("--allow-growth", action="store_true")
    parser.add_argument("--quiet-when-clean", action="store_true")
    parser.add_argument("--repo-root", default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(list(argv) if argv is not None else None)


def _error(message: str) -> None:
    print(f"[private-import] {message}", file=sys.stderr, flush=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        result = scan(args.repo_root)
    except SyntaxError as exc:
        _error(f"源码解析失败，门禁已阻断：{exc}")
        return 2
    current = entries_to_counts(list(result["entries"]))  # type: ignore[arg-type]
    baseline_path = default_baseline_path(args.repo_root, RULE)
    if args.refresh:
        try:
            previous = load_baseline(baseline_path, expected_rule=RULE)
        except BoundaryBaselineError as exc:
            _error(str(exc))
            return 2
        if previous is not None and total_count(current) > total_count(previous) and not args.allow_growth:
            _error(f"刷新被拒绝：总命中数 {total_count(previous)} -> {total_count(current)} 增长；请先消除新增债务，或人工核对后加 --allow-growth")
            return 1
        write_baseline(baseline_path, rule=RULE, scan_roots=list(SCAN_ROOTS), note=BASELINE_NOTE, counts=current)
        print(f"[{RULE}] 基线已写入 {os.path.relpath(baseline_path, args.repo_root)}：{len(current)} 条目 / {total_count(current)} 处")
        return 0
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not args.fail_on_new:
        print(render_text(result), flush=True)
        return 0
    try:
        baseline = load_baseline(baseline_path, expected_rule=RULE)
    except BoundaryBaselineError as exc:
        _error(str(exc))
        return 2
    comparison = compare_counts(current, baseline)
    if comparison.baseline_missing:
        _error(f"基线缺失：{baseline_path}；请先查看扫描结果并受控执行 --refresh")
        return 2
    if comparison.clean and args.quiet_when_clean:
        return 0
    for line in render_comparison(RULE, comparison):
        print(line, flush=True)
    return 1 if comparison.has_debt else 0


if __name__ == "__main__":
    raise SystemExit(main())
