"""SCIP 精确引用确认。

本模块只服务 ``scan_dead_code_islands.py --mode precise``。
``load_precise_index`` 是精确模式的前置检查与读取入口（工作区干净、scip CLI、索引文件、索引版本是 HEAD），
扫描器在跑调用图之前调用它；前置条件不满足时抛明确错误,不回退到快模式。
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .model import CodeRoles, FunctionRecord, UsageEvidence

SCIP_INDEX = os.path.join(".checkup", "scip", "index.scip")
DEFINITION_ROLE = 1
_VERSION_RE = re.compile(r"\b[A-Za-z0-9_.-]+\s+([0-9a-f]{40})\b")


class ScipUsageError(Exception):
    def __init__(self, code: str, message: str, hints: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = list(hints)


def load_precise_index(repo_root: str, index_path: Optional[str] = None) -> List[Dict]:
    """检查精确模式的前置条件并读出索引文档；任何一项不满足都抛 ScipUsageError。"""
    return _load_documents(repo_root, _require_fresh_index(repo_root, index_path=index_path))


def collect_scip_usage(repo_root: str, docs: List[Dict], records: Dict[str, FunctionRecord],
                       roles: CodeRoles) -> Dict[str, List[UsageEvidence]]:
    """docs 是 load_precise_index 读出的索引文档；证据来源取引用所在文件的角色（roles.of）。"""
    symbols = _definition_symbols(docs, records)
    return _references_for_symbols(repo_root, docs, symbols, roles)


def _require_fresh_index(repo_root: str, index_path: Optional[str] = None) -> str:
    if _git_dirty(repo_root):
        raise ScipUsageError(
            "dirty_worktree",
            "精确模式要求工作区干净,否则 SCIP 索引不能代表当前源码。",
            ["先提交或清理当前改动后重试。"],
        )
    if shutil.which("scip") is None:
        raise ScipUsageError(
            "missing_scip_cli",
            "未找到 scip CLI,无法做精确引用确认。",
            ["安装 scip CLI 后重试。"],
        )
    path = os.path.abspath(os.path.join(repo_root, index_path or SCIP_INDEX))
    if not os.path.isfile(path):
        raise ScipUsageError(
            "missing_index",
            f"未找到 SCIP 索引文件:{path}",
            [
                "先运行: python -m tools.symbol_locator build-index",
                "或运行: scip-python index . --project-name=project --output .checkup/scip/index.scip",
            ],
        )
    return path


def _git_dirty(repo_root):
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True)
    return proc.returncode != 0 or bool(proc.stdout.strip())


def _load_documents(repo_root, path):
    proc = subprocess.run(["scip", "print", "--json", path], cwd=repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ScipUsageError(
            "scip_print_failed",
            "scip print --json 读取索引失败:%s" % (proc.stderr.strip() or proc.stdout.strip()),
            ["确认 scip CLI 版本可读取当前 index.scip。"],
        )
    docs = _parse_scip_documents(proc.stdout)
    if not isinstance(docs, list):
        raise ScipUsageError("scip_json_invalid", "scip print JSON 中没有 documents 列表。")
    _require_index_version(repo_root, docs)
    return docs


def _parse_scip_documents(text: str) -> List[Dict]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        payload = json.loads(stripped)
    except ValueError:
        return _parse_ndjson(stripped.splitlines())
    if isinstance(payload, dict):
        docs = payload.get("documents")
        if isinstance(docs, list):
            return docs
        if "occurrences" in payload:
            return [payload]
    if isinstance(payload, list):
        return payload
    raise ScipUsageError("scip_json_invalid", "scip print JSON 中没有 documents 列表。")


def _parse_ndjson(lines: Iterable[str]) -> List[Dict]:
    docs = []
    try:
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            item = json.loads(raw)
            if isinstance(item, dict):
                docs.append(item)
    except ValueError as exc:
        raise ScipUsageError("scip_json_invalid", "scip print 输出不是合法 JSON。", [str(exc)]) from exc
    return docs


def _require_index_version(repo_root, docs):
    head = _git_head(repo_root)
    versions = set()
    for doc in docs:
        for occ in doc.get("occurrences") or []:
            symbol = str(occ.get("symbol") or "")
            versions.update(_VERSION_RE.findall(symbol))
    if head not in versions:
        sample = ", ".join(sorted(versions)[:3]) or "未发现项目版本号"
        raise ScipUsageError(
            "stale_index",
            f"SCIP 索引不是当前 HEAD。当前 HEAD={head[:12]},索引版本={sample}",
            ["重新运行: python -m tools.symbol_locator build-index"],
        )


def _git_head(repo_root):
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ScipUsageError("git_head_failed", "无法读取当前 git HEAD。", [proc.stderr.strip()])
    return proc.stdout.strip()


def _definition_symbols(docs: List[Dict], records: Dict[str, FunctionRecord]) -> Dict[str, Set[str]]:
    by_location: Dict[Tuple[str, int], List[str]] = defaultdict(list)
    for qual, record in records.items():
        by_location[(record.rel, record.line)].append(qual)
    result: Dict[str, Set[str]] = defaultdict(set)
    for doc in docs:
        path = _path(doc)
        for occ in doc.get("occurrences") or []:
            if not _roles(occ) & DEFINITION_ROLE:
                continue
            symbol = str(occ.get("symbol") or "")
            for qual in by_location.get((path, _line(occ)), ()):
                if _matches_record_symbol(symbol, records[qual]):
                    result[qual].add(symbol)
    return result


def _references_for_symbols(
    repo_root: str, docs: List[Dict], symbols_by_qual: Dict[str, Set[str]], roles: CodeRoles
) -> Dict[str, List[UsageEvidence]]:
    grouped = defaultdict(list)
    targets = {}
    import_cache: Dict[str, List[Tuple[int, int, int, int]]] = {}
    for qual, symbols in symbols_by_qual.items():
        for symbol in symbols:
            targets[symbol] = qual
    if not targets:
        return {}
    for doc in docs:
        path = _path(doc)
        for occ in doc.get("occurrences") or []:
            symbol = str(occ.get("symbol") or "")
            target = targets.get(symbol)
            if not target or (_roles(occ) & DEFINITION_ROLE):
                continue
            if _is_import_reference(repo_root, path, occ, import_cache):
                continue
            grouped[target].append(
                UsageEvidence(
                    target=target,
                    source_rel=path,
                    line=_line(occ),
                    kind="scip_reference",
                    detail=symbol,
                    origin=roles.of(path),
                )
            )
    return {qual: rows for qual, rows in grouped.items()}


def _is_import_reference(repo_root: str, rel: str, occ: Dict, import_cache: Dict[str, List[Tuple[int, int, int, int]]]) -> bool:
    rng = occ.get("range") or []
    if len(rng) < 2:
        return False
    if rel not in import_cache:
        import_cache[rel] = _import_reference_ranges(repo_root, rel)
    line_no = int(rng[0]) + 1
    col = int(rng[1])
    for start_line, start_col, end_line, end_col in import_cache[rel]:
        if _contains_position(line_no, col, start_line, start_col, end_line, end_col):
            return True
    return False


def _import_reference_ranges(repo_root: str, rel: str) -> List[Tuple[int, int, int, int]]:
    """索引与干净的 HEAD 工作区一致，索引里的文件必然存在且能解析；读不到就是真实故障，直接抛出。"""
    with open(os.path.join(repo_root, rel), encoding="utf-8") as handle:
        text = handle.read()
    tree = ast.parse(text)
    lines = text.splitlines()
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = _imported_names(node)
            ranges.extend(_name_ranges_in_import(lines, node, names))
    return ranges


def _imported_names(node: Union[ast.Import, ast.ImportFrom]) -> Set[str]:
    names = set()
    for alias in node.names:
        names.add(alias.name.split(".")[-1])
        if alias.asname:
            names.add(alias.asname)
    return names


def _name_ranges_in_import(
    lines: List[str], node: Union[ast.Import, ast.ImportFrom], names: Set[str]
) -> List[Tuple[int, int, int, int]]:
    start_line = node.lineno
    end_line = node.end_lineno
    end_col = node.end_col_offset
    assert end_line is not None and end_col is not None
    ranges = []
    for line_no in range(start_line, end_line + 1):
        raw = lines[line_no - 1]
        segment_start = node.col_offset if line_no == start_line else 0
        segment_end = end_col if line_no == end_line else len(raw)
        segment = raw[segment_start:segment_end]
        for name in names:
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
            for match in re.finditer(pattern, segment):
                ranges.append((line_no, segment_start + match.start(), line_no, segment_start + match.end()))
    return ranges


def _contains_position(line_no: int, col: int, start_line: int, start_col: int, end_line: int, end_col: int) -> bool:
    if line_no < start_line or line_no > end_line:
        return False
    if line_no == start_line and col < start_col:
        return False
    if line_no == end_line and col >= end_col:
        return False
    return True


def _matches_record_symbol(symbol: str, record: FunctionRecord) -> bool:
    if record.cls:
        return f"#{record.name}()." in symbol and f"/{record.cls}#" in symbol
    return symbol.endswith(f"/{record.name}().")


def _path(doc):
    return str(doc.get("relative_path") or doc.get("path") or "")


def _roles(occ):
    return int(occ.get("symbol_roles") or occ.get("symbolRoles") or 0)


def _line(occ):
    rng = occ.get("range") or []
    return int(rng[0]) + 1 if rng else 0
