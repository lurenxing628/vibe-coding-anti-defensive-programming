"""失效仓库路径字面量扫描器（dead_path_literal 棘轮规则）。

要解决的问题：退役删文件时，代码里写死这个文件路径的字符串不会报错。
2026-09-18 删旧路由层后，`tests/web_pages/test_frontend_ui_language_polish.py`
里 12 个 `_read("web/routes/...")` 指向了不存在的文件，而该文件整体被标 perf
不进任何门禁，烂了三天没人发现。同一天 `tests/gate_meta/test_run_quality_gate.py`
的一条断言也因为目标文件被删而变成永远通过的空断言。

口径：
- 扫描 core/ web/ data/ tools/ scripts/ tests/ 与仓库根下的 .py。
- 候选：字符串常量里含 `/`、首段是仓库顶层目录、带文件扩展名。
- 不计入：运行时产物目录（evidence/ db/ logs/ backups/ tmp/ dist/ build/
  user-data/ output/）、含空白或通配或格式占位的字符串、绝对路径与 URL。
  这些要么是运行时才生成，要么根本不是路径。
- 也不计入含按白名单局部忽略的本机历史归档目录（如本机忽略归档目录）
  与本地生成的全景图样式/脚本（清单是 tools/git_hook_blocked_paths.py 的 LOCAL_PANORAMA_FILES）。
  pre-push 在干净 worktree 里跑本扫描，结果必须与主工作区一致。
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
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

from tools.boundary_baseline import add_ratchet_arguments, run_ratchet_cli
from tools.path_roles import iter_files

RULE = "dead_path_literal"
SCAN_ROOTS = ("src", "core", "web", "data", "app", "tools", "scripts", "tests", "*.py")
BASELINE_NOTE = "失效仓库路径字面量棘轮基线：删文件必须同步删掉引用它的字符串；条目只减不增，刷新走 --refresh。"

#: 仓库顶层目录。首段不在这里的字符串不当成仓库路径，避免把 URL 片段、
#: 包名、SQL 里的斜杠误判成路径。本机忽略目录不纳入扫描。
REPO_TOP_DIRS = frozenset({
    "core", "web", "data", "tools", "scripts", "tests", "static", "templates",
    "frontend", "installer", "assets", "docs", "src", "app", "skills", ".github",
})

#: 运行时才生成的目录，源码里写它们的路径是正常的，不算失效。
RUNTIME_PREFIXES = (
    "evidence/", "db/", "logs/", "backups/", "tmp/", "dist/", "build/",
    "user-data/", "output/",
)

#: 当成仓库路径的字符串必须带的扩展名。
PATH_LITERAL_EXTENSIONS = (
    "py", "js", "jsx", "cjs", "json", "html", "css", "md", "xlsx", "txt", "bat", "iss", "yaml", "yml", "cfg",
    "toml", "ini",
)
_EXTENSION = re.compile(r"\.(" + "|".join(PATH_LITERAL_EXTENSIONS) + r")$")
#: 判定结果取决于哪些路径存在：顶层目录 × 扩展名。门禁规格把它登记为只看存在性的输入
#: （`GateCheckSpec.existence_file_scopes`），增删这些文件时日常门禁触发本扫描。
EXISTENCE_FILE_SCOPES = tuple(f"{top}/**/*.{ext}" for top in sorted(REPO_TOP_DIRS) for ext in PATH_LITERAL_EXTENSIONS)

#: 含这些字符的就不是一条确定的仓库路径：空白和逗号多半是散文，
#: 通配和格式占位是模式不是路径，反斜杠是 Windows 路径另说。
_NOT_A_PLAIN_PATH = re.compile(r"[\s,*?{}$\\<>|]")


def is_repo_path_literal(value: str) -> bool:
    # 注意别把 .github/ 当成相对路径前缀一起排掉，它是仓库顶层目录；
    # 要排的只有 ./ 与 ../ 这两种写法。本机忽略目录靠 REPO_TOP_DIRS 不收录来排除。
    if "/" not in value or value.startswith(("/", "http", "~", "./", "../")):
        return False
    if _NOT_A_PLAIN_PATH.search(value):
        return False
    if value.startswith(RUNTIME_PREFIXES):
        return False
    return value.split("/", 1)[0] in REPO_TOP_DIRS and bool(_EXTENSION.search(value))


def scan_dead_path_literals_file(rel_path: str, source: str, exists) -> List[Dict[str, Any]]:
    tree = ast.parse(source, filename=rel_path)
    hits: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        literal = node.value
        if is_repo_path_literal(literal) and not exists(literal):
            hits.append({"kind": literal, "line": node.lineno})
    return hits


def scan(repo_root: str) -> Dict[str, Any]:
    def exists(literal: str) -> bool:
        return os.path.exists(os.path.join(repo_root, literal))

    entries: List[Dict[str, Any]] = []
    files = iter_files(repo_root, SCAN_ROOTS, (".py",))
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


def render_text(result: Dict[str, Any]) -> str:
    entries = result["entries"]
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
    add_ratchet_arguments(parser)
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
    return run_ratchet_cli(
        args,
        rule=RULE,
        result=result,
        note=BASELINE_NOTE,
        render_text=render_text,
        error_fn=_error,
    )


if __name__ == "__main__":
    raise SystemExit(main())
