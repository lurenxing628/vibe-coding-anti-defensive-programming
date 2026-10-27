"""SCIP 精确引用确认。

本模块只服务 ``scan_dead_code_islands.py --mode precise``。
精确模式的前置条件不满足时抛明确错误,不回退到快模式。
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .model import FunctionRecord, UsageEvidence

SCIP_INDEX = os.path.join(".checkup", "scip", "index.scip")
DEFINITION_ROLE = 1
_VERSION_RE = re.compile(r"\b[A-Za-z0-9_.-]+\s+([0-9a-f]{40})\b")


class ScipUsageError(Exception):
    def __init__(self, code, message, hints=None):
        # type: (str, str, Optional[List[str]]) -> None
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = list(hints or [])


def collect_scip_usage(repo_root, records, index_path=None):
    # type: (str, Dict[str, FunctionRecord], Optional[str]) -> Dict[str, List[UsageEvidence]]
    path = _require_fresh_index(repo_root, index_path=index_path)
    docs = _load_documents(repo_root, path)
    symbols = _definition_symbols(docs, records)
    return _references_for_symbols(repo_root, docs, symbols)


def _require_fresh_index(repo_root, index_path=None):
    # type: (str, Optional[str]) -> str
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


def _parse_scip_documents(text):
    # type: (str) -> List[Dict]
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


def _parse_ndjson(lines):
    # type: (Iterable[str]) -> List[Dict]
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
        sample = ", ".join(sorted(versions)[:3]) or "未发现索引版本号"
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


def _definition_symbols(docs, records):
    # type: (List[Dict], Dict[str, FunctionRecord]) -> Dict[str, Set[str]]
    by_location = defaultdict(list)
    for qual, record in records.items():
        by_location[(record.rel, record.line, record.name, record.cls)].append(qual)
    result = defaultdict(set)  # type: Dict[str, Set[str]]
    for doc in docs:
        path = _path(doc)
        for occ in doc.get("occurrences") or []:
            roles = _roles(occ)
            if not roles & DEFINITION_ROLE:
                continue
            line = _line(occ)
            symbol = str(occ.get("symbol") or "")
            for qual, record in records.items():
                if path != record.rel or line != record.line:
                    continue
                if _matches_record_symbol(symbol, record):
                    result[qual].add(symbol)
    return result


def _references_for_symbols(repo_root, docs, symbols_by_qual):
    # type: (str, List[Dict], Dict[str, Set[str]]) -> Dict[str, List[UsageEvidence]]
    grouped = defaultdict(list)
    targets = {}
    import_cache = {}  # type: Dict[str, List[Tuple[int, int, int, int]]]
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
                )
            )
    return {qual: rows for qual, rows in grouped.items()}


def _is_import_reference(repo_root, rel, occ, import_cache):
    # type: (str, str, Dict, Dict[str, List[Tuple[int, int, int, int]]]) -> bool
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


def _import_reference_ranges(repo_root, rel):
    # type: (str, str) -> List[Tuple[int, int, int, int]]
    text = _read_source(repo_root, rel)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = _imported_names(node)
            ranges.extend(_name_ranges_in_import(lines, node, names))
    return ranges


def _read_source(repo_root, rel):
    # type: (str, str) -> Optional[str]
    try:
        with open(os.path.join(repo_root, rel), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _imported_names(node):
    # type: (ast.AST) -> Set[str]
    names = set()
    for alias in getattr(node, "names", []):
        raw = str(getattr(alias, "name", "") or "")
        if raw:
            names.add(raw.split(".")[-1])
        asname = getattr(alias, "asname", None)
        if asname:
            names.add(str(asname))
    return names


def _name_ranges_in_import(lines, node, names):
    # type: (List[str], ast.AST, Set[str]) -> List[Tuple[int, int, int, int]]
    if not names:
        return []
    start_line = int(getattr(node, "lineno", 0) or 0)
    end_line = int(getattr(node, "end_lineno", start_line) or start_line)
    start_col = int(getattr(node, "col_offset", 0) or 0)
    end_col = getattr(node, "end_col_offset", None)
    ranges = []
    for line_no in range(start_line, end_line + 1):
        if line_no <= 0 or line_no > len(lines):
            continue
        raw = lines[line_no - 1]
        segment_start = start_col if line_no == start_line else 0
        if line_no == end_line and end_col is not None:
            segment_end = int(end_col)
        else:
            segment_end = len(raw)
        segment = raw[segment_start:segment_end]
        for name in names:
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
            for match in re.finditer(pattern, segment):
                ranges.append((line_no, segment_start + match.start(), line_no, segment_start + match.end()))
    return ranges


def _contains_position(line_no, col, start_line, start_col, end_line, end_col):
    # type: (int, int, int, int, int, int) -> bool
    if line_no < start_line or line_no > end_line:
        return False
    if line_no == start_line and col < start_col:
        return False
    if line_no == end_line and col >= end_col:
        return False
    return True


def _matches_record_symbol(symbol, record):
    # type: (str, FunctionRecord) -> bool
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
