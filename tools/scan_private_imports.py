"""私有名越界扫描器（private_import 棘轮规则）。

棘轮口径
- kind=private_symbol：`from <模块> import _name`。豁免：目标模块最后一段本身以 `_` 开头（私有模块）、
  `_frozen_import_anchor`、`from __future__`。
- kind=private_name_exported：模块作用域（含 if/try/with/for/while 控制块，不进函数体与类体）里赋给 `__all__`
  的列表/元组字面量中的私有名（私有名不得作为公开导出）。
- 每个文件按 (path, kind) 计数；基线只减不增。

用法：
    python -m tools.scan_private_imports [--json] [--fail-on-new] [--refresh [--allow-growth]] [--quiet-when-clean]
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from tools.boundary_baseline import add_ratchet_arguments, run_ratchet_cli
from tools.path_roles import iter_files

RULE = "private_import"
SCAN_ROOTS = ("src", "core", "web", "data", "app")
KIND_LABELS = {
    "private_symbol": "跨模块私有导入",
    "private_name_exported": "__all__ 导出私有名",
}
BASELINE_NOTE = (
    "禁跨模块导入私有符号、禁 __all__ 导出私有名的棘轮基线：拆文件必须给共享部分起公开名字；"
    "条目只减不增，刷新走 --refresh。"
)


class PrivateNameHit(NamedTuple):
    kind: str
    line: int
    symbol: str


def _is_private_symbol(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _exported_private_names(statement: ast.AST) -> List[PrivateNameHit]:
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        targets = [statement.target]
    else:
        return []
    if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
        return []
    if not isinstance(statement.value, (ast.List, ast.Tuple)):
        return []
    return [
        PrivateNameHit("private_name_exported", element.lineno, element.value)
        for element in statement.value.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str) and _is_private_symbol(element.value)
    ]


def scan_private_imports_file(rel_path: str, source: str) -> List[PrivateNameHit]:
    tree = ast.parse(source, filename=rel_path)
    hits: List[PrivateNameHit] = []
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
                hits.append(PrivateNameHit("private_symbol", node.lineno, alias.name))
    # 模块作用域包括 if/try/with/for/while 等控制块里的语句（含 else、finally、except 分支）；函数体与类体是各自的作用域。
    module_scope: List[ast.AST] = list(tree.body)
    while module_scope:
        node = module_scope.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        hits.extend(_exported_private_names(node))
        module_scope.extend(
            child for child in ast.iter_child_nodes(node) if isinstance(child, (ast.stmt, ast.excepthandler))
        )
    return hits


def scan(repo_root: str) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    files = iter_files(repo_root, SCAN_ROOTS, (".py",))
    for rel_path in files:
        with open(os.path.join(repo_root, rel_path), encoding="utf-8") as handle:
            hits = scan_private_imports_file(rel_path, handle.read())
        hits_by_kind: Dict[str, List[PrivateNameHit]] = defaultdict(list)
        for hit in hits:
            hits_by_kind[hit.kind].append(hit)
        for kind, kind_hits in sorted(hits_by_kind.items()):
            entries.append(
                {
                    "path": rel_path,
                    "kind": kind,
                    "count": len(kind_hits),
                    "lines": sorted(hit.line for hit in kind_hits),
                    "symbols": sorted({hit.symbol for hit in kind_hits}),
                }
            )
    return {"rule": RULE, "scan_roots": list(SCAN_ROOTS), "file_count": len(files), "entries": entries,
            "total": sum(entry["count"] for entry in entries)}


def render_text(result: Dict[str, Any]) -> str:
    entries = result["entries"]
    totals: Counter[str] = Counter()
    for entry in entries:
        totals[entry["kind"]] += entry["count"]
    summary = "，".join(f"{label} {totals[kind]} 处" for kind, label in KIND_LABELS.items())
    lines = [f"[{RULE}] 扫描 {result['file_count']} 个文件，{summary}，共 {len(entries)} 个条目"]
    for entry in sorted(entries, key=lambda item: (-item["count"], item["path"], item["kind"]))[:15]:
        lines.append(
            f"  {entry['count']:3d}  {entry['path']}  {KIND_LABELS[entry['kind']]}  ({', '.join(entry['symbols'][:5])})"
        )
    return "\n".join(lines)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="私有名越界棘轮扫描器（跨模块私有导入、__all__ 导出私有名）")
    add_ratchet_arguments(parser)
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
