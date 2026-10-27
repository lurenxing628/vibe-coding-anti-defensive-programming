"""失效仓库路径字面量扫描器（dead_path_literal 棘轮规则）。

要解决的问题：退役删文件时，代码或测试里写死这个文件路径的字符串不会报错，
导致读取不存在的文件或触发永远通过的空断言。

口径：
- 扫描源码目录、工具脚本与测试目录下的 .py。
- 候选：字符串常量里含 `/`、首段是仓库顶层目录、带文件扩展名。
- 不计入：运行时产物目录（evidence/ db/ logs/ backups/ tmp/ dist/ build/
  user-data/ output/）、含空白或通配或格式占位的字符串、绝对路径与 URL。
  这些要么是运行时才生成，要么根本不是路径。
- 条目键是 (源文件, 那条失效路径)，count 是出现次数。门禁元测试在临时目录里
  合成假仓库（core/services/example.py 之类）是正常做法，它们进基线当存量；
  基线只减不增，所以新删一个真文件必定报红。

用法：
    python -m tools.scan_dead_path_literals [--json] [--fail-on-new]
        [--refresh [--allow-growth]] [--quiet-when-clean]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
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
RULE = "dead_path_literal"
SCAN_ROOTS = ("src", "core", "web", "data", "app", "tools", "scripts", "tests", "plugins")
BASELINE_NOTE = "失效仓库路径字面量棘轮基线：删文件必须同步删掉引用它的字符串；条目只减不增，刷新走 --refresh。"

#: 仓库顶层目录。首段不在这里的字符串不当成仓库路径，避免把 URL 片段、
#: 包名、SQL 里的斜杠误判成路径。
REPO_TOP_DIRS = frozenset({
    "src", "core", "web", "data", "app", "tools", "scripts", "tests", "static", "templates",
    "frontend", "plugins", "installer", "assets", "docs", ".github",
})

#: 运行时才生成的目录，源码里写它们的路径是正常的，不算失效。
RUNTIME_PREFIXES = (
    "evidence/", "db/", "logs/", "backups/", "tmp/", "dist/", "build/",
    "user-data/", "output/",
)
LOCAL_GENERATED_PATHS: frozenset = frozenset()

_EXTENSION = re.compile(r"\.(py|js|jsx|cjs|json|html|css|md|xlsx|txt|bat|iss|ya?ml|cfg|toml|ini)$")
#: 含这些字符的就不是一条确定的仓库路径：空白和逗号多半是散文，
#: 通配和格式占位是模式不是路径，反斜杠是 Windows 路径另说。
_NOT_A_PLAIN_PATH = re.compile(r"[\s,*?{}$\\<>|]")


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
    for filename in sorted(os.listdir(repo_root)):
        if filename.endswith(".py") and os.path.isfile(os.path.join(repo_root, filename)):
            files.append(filename)
    return sorted(set(files))


def is_repo_path_literal(value: str) -> bool:
    # 注意别把 .github/ 当成相对路径前缀一起排掉，它是仓库顶层目录；
    # 要排的只有 ./ 与 ../ 这两种写法。
    if "/" not in value or value.startswith(("/", "http", "~", "./", "../")):
        return False
    if _NOT_A_PLAIN_PATH.search(value):
        return False
    if value.startswith(RUNTIME_PREFIXES) or value in LOCAL_GENERATED_PATHS:
        return False
    return value.split("/", 1)[0] in REPO_TOP_DIRS and bool(_EXTENSION.search(value))


def scan_dead_path_literals_file(rel_path: str, source: str, exists) -> List[Dict[str, object]]:
    tree = ast.parse(source, filename=rel_path)
    hits: List[Dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        literal = node.value
        if is_repo_path_literal(literal) and not exists(literal):
            hits.append({"kind": literal, "line": node.lineno})
    return hits


def scan(repo_root: str = REPO_ROOT) -> Dict[str, object]:
    def exists(literal: str) -> bool:
        return os.path.exists(os.path.join(repo_root, literal))

    entries: List[Dict[str, object]] = []
    files = _iter_python_files(repo_root, SCAN_ROOTS)
    for rel_path in files:
        with open(os.path.join(repo_root, rel_path), encoding="utf-8") as handle:
            hits = scan_dead_path_literals_file(rel_path, handle.read(), exists)
        by_literal: Dict[str, List[int]] = {}
        for hit in hits:
            by_literal.setdefault(str(hit["kind"]), []).append(int(hit["line"]))
        for literal in sorted(by_literal):
            lines = sorted(by_literal[literal])
            entries.append({"path": rel_path, "kind": literal, "count": len(lines), "lines": lines})
    return {"rule": RULE, "scan_roots": list(SCAN_ROOTS), "file_count": len(files), "entries": entries,
            "total": sum(int(entry["count"]) for entry in entries)}


def render_text(result: Dict[str, object]) -> str:
    entries = list(result.get("entries") or [])
    by_file: Dict[str, int] = {}
    for entry in entries:
        path = str(entry["path"])
        by_file[path] = by_file.get(path, 0) + int(entry["count"])
    lines = [f"[{RULE}] 扫描 {result['file_count']} 个文件，失效路径字面量 {result['total']} 处，涉及 {len(by_file)} 个文件"]
    for path in sorted(by_file, key=lambda item: (-by_file[item], item))[:15]:
        samples = [str(entry["kind"]) for entry in entries if str(entry["path"]) == path][:3]
        lines.append(f"  {by_file[path]:3d}  {path}  ({', '.join(samples)})")
    return "\n".join(lines)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="失效仓库路径字面量棘轮扫描器")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-new", action="store_true", help="相对基线有新增/增加/失效条目即退出码 1；基线异常退出码 2")
    parser.add_argument("--refresh", action="store_true", help="按当前扫描结果重写基线（默认拒绝总命中数增长）")
    parser.add_argument("--allow-growth", action="store_true")
    parser.add_argument("--quiet-when-clean", action="store_true")
    parser.add_argument("--repo-root", default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(list(argv) if argv is not None else None)


def _error(message: str) -> None:
    print(f"[dead-path-literal] {message}", file=sys.stderr, flush=True)


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
        if previous is not None and not args.allow_growth:
            # 光比总数挡不住"还掉一笔、同时欠下一笔"：总数不变，新的失效字面量就被烘进基线了。
            # 刷新只该用来清理已还的债，新增一律要人工核对后显式 --allow-growth。
            comparison = compare_counts(current, previous)
            if comparison.new or comparison.increased:
                detail = sorted(comparison.new) + sorted(comparison.increased)
                _error("刷新被拒绝：有新增的失效路径字面量，请先改掉引用而不是刷基线；"
                       "确属测试夹具等人工核对过的情况再加 --allow-growth。\n  "
                       + "\n  ".join(f"{path} -> {literal}" for path, literal in detail))
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
