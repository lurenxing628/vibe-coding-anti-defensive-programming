"""SCIP 深度索引查询层。

本模块只在 ``--deep`` 路径导入/调用外部 ``scip`` / ``scip-python`` 命令。
默认静态图路径不依赖 Node 或 SCIP 工具链。
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional

from .static_index import REPO_ROOT

SCIP_DIR = os.path.join(REPO_ROOT, ".checkup", "scip")
INDEX_FILE = os.path.join(SCIP_DIR, "index.scip")
DEFINITION_ROLE = 1


class ScipUnavailable(Exception):
    """``--deep`` 需要的外部命令或索引不存在。"""

    def __init__(self, code, message, hints=None):
        # type: (str, str, Optional[List[str]]) -> None
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = list(hints or [])


def ensure_index(index_path=None):
    # type: (Optional[str]) -> str
    path = os.path.abspath(index_path or INDEX_FILE)
    if shutil.which("scip") is None:
        raise ScipUnavailable(
            "missing_scip_cli",
            "未找到 scip CLI,无法读取 SCIP 索引。",
            [
                "安装 scip CLI 后重试。",
                "macOS arm64 可从 https://github.com/scip-code/scip/releases 下载 scip-darwin-arm64.tar.gz。",
            ],
        )
    if not os.path.exists(path):
        raise ScipUnavailable(
            "missing_index",
            f"未找到 SCIP 索引文件:{path}",
            [
                "先运行: mkdir -p .checkup/scip",
                "再运行: scip-python index . --project-name=project --output .checkup/scip/index.scip",
                "若仓库提供 scip-pyrightconfig.json,scip-python 会优先使用它把 tests/tools/scripts 纳入索引。",
            ],
        )
    return path


def build_index(index_path=None, stdout=None):
    # type: (Optional[str], Any) -> str
    """用 scip-python 重建索引。成功返回索引绝对路径。"""
    if shutil.which("scip-python") is None:
        raise ScipUnavailable(
            "missing_scip_python",
            "未找到 scip-python,无法生成 SCIP 索引。",
            ["安装命令:npm i -g @sourcegraph/scip-python"],
        )
    path = os.path.abspath(index_path or INDEX_FILE)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    subprocess.run(
        [
            "scip-python",
            "index",
            ".",
            "--project-name=project",
            "--output",
            path,
        ],
        cwd=REPO_ROOT,
        check=True,
        stdout=stdout,
    )
    return path


def load_documents(index_path=None):
    # type: (Optional[str]) -> Dict
    path = ensure_index(index_path=index_path)
    proc = subprocess.run(
        ["scip", "print", "--json", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise ScipUnavailable(
            "scip_print_failed",
            "scip print --json 读取索引失败:%s" % (proc.stderr.strip() or proc.stdout.strip()),
            ["确认 scip CLI 版本可读取当前 index.scip。"],
        )
    docs = _parse_scip_print(proc.stdout)
    return {
        "path": path,
        "mtime": os.path.getmtime(path),
        "documents": docs,
    }


def query(name, direction, static_index, index_path=None):
    # type: (str, str, object, Optional[str]) -> Dict
    data = load_documents(index_path=index_path)
    docs = data["documents"]
    defs = _definitions_for_name(docs, name, static_index)
    if direction == "whereis":
        rows = defs
    elif direction == "callers":
        rows = _callers_for_defs(docs, defs)
    elif direction == "callees":
        rows = _callees_for_defs(docs, defs, static_index)
    else:
        raise ValueError("unknown direction: " + direction)
    return {
        "engine": "scip",
        "symbol": name,
        "direction": direction,
        "index": {
            "path": data["path"],
            "mtime": data["mtime"],
            "label": time.strftime("%Y-%m-%d %H:%M", time.localtime(data["mtime"])),
        },
        "definitions": defs,
        "rows": rows,
    }


def _parse_scip_print(text):
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
    return []


def _parse_ndjson(lines):
    # type: (Iterable[str]) -> List[Dict]
    docs = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        item = json.loads(raw)
        if isinstance(item, dict):
            docs.append(item)
    return docs


def _symbol_pattern(name):
    # type: (str) -> re.Pattern
    # 精确匹配函数/方法符号,排除 ``foo().(param)`` 这类参数符号。
    return re.compile(r"[/#]" + re.escape(name) + r"\(\)\.$")


def _is_target_symbol(symbol, name):
    # type: (str, str) -> bool
    return bool(_symbol_pattern(name).search(symbol))


def _is_callable_symbol(symbol):
    # type: (str) -> bool
    return bool(re.search(r"[/#][A-Za-z_][A-Za-z0-9_]*\(\)\.$", symbol))


def _roles(occ):
    # type: (Dict) -> int
    return int(occ.get("symbol_roles") or occ.get("symbolRoles") or 0)


def _path(doc):
    # type: (Dict) -> str
    return str(doc.get("relative_path") or doc.get("path") or "")


def _line(occ):
    # type: (Dict) -> int
    rng = occ.get("range") or []
    if not rng:
        return 0
    return int(rng[0]) + 1


def _range_key(occ):
    # type: (Dict) -> str
    rng = occ.get("range") or []
    return ":".join(str(part) for part in rng)


def _row(doc, occ, target_symbol=None):
    # type: (Dict, Dict, Optional[str]) -> Dict
    symbol = str(occ.get("symbol") or "")
    return {
        "path": _path(doc),
        "line": _line(occ),
        "range": list(occ.get("range") or []),
        "symbol": symbol,
        "display": display_symbol(symbol),
        "target_symbol": target_symbol or symbol,
    }


def _definitions_for_name(docs, name, static_index):
    # type: (List[Dict], str, object) -> List[Dict]
    static_targets = _static_definition_locations(name, static_index)
    rows = []
    for doc in docs:
        for occ in doc.get("occurrences") or []:
            symbol = str(occ.get("symbol") or "")
            if _is_target_symbol(symbol, name) and (_roles(occ) & DEFINITION_ROLE):
                row = _row(doc, occ)
                if static_targets and (row["path"], row["line"]) not in static_targets:
                    continue
                rows.append(row)
    return _dedupe_rows(rows)


def _static_definition_locations(name, static_index):
    # type: (str, object) -> set
    result = set()
    for key in static_index.lookup_name(name):
        info = static_index.info(key)
        if info:
            result.add((info["rel"], int(info["line"])))
    return result


def _callers_for_defs(docs, defs):
    # type: (List[Dict], List[Dict]) -> List[Dict]
    targets = set(row["symbol"] for row in defs)
    rows = []
    call_cache = {}
    for doc in docs:
        for occ in doc.get("occurrences") or []:
            symbol = str(occ.get("symbol") or "")
            if (symbol in targets and not (_roles(occ) & DEFINITION_ROLE)
                    and _is_call_occurrence(doc, occ, call_cache)):
                rows.append(_row(doc, occ, target_symbol=symbol))
    return _dedupe_rows(rows)


def _is_call_occurrence(doc, occ, call_cache):
    # type: (Dict, Dict, Dict[str, List]) -> bool
    """SCIP 只告诉我们“这里引用了目标符号”,这里再用 AST 确认它真在调用位。"""
    rng = occ.get("range") or []
    if len(rng) < 2:
        return False
    path = _path(doc)
    if path not in call_cache:
        call_cache[path] = _call_func_ranges(path)
    line_no = int(rng[0]) + 1
    col = int(rng[1])
    for start_line, start_col, end_line, end_col in call_cache[path]:
        if _contains_position(line_no, col, start_line, start_col, end_line, end_col):
            return True
    return False


def _call_func_ranges(path):
    # type: (str) -> List
    text = _read_source(path)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    ranges = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        start_line = getattr(func, "lineno", None)
        start_col = getattr(func, "col_offset", None)
        end_line = getattr(func, "end_lineno", None)
        end_col = getattr(func, "end_col_offset", None)
        if None in (start_line, start_col, end_line, end_col):
            continue
        ranges.append((int(start_line), int(start_col), int(end_line), int(end_col)))
    return ranges


def _read_source(path):
    # type: (str) -> Optional[str]
    full = os.path.join(REPO_ROOT, path)
    try:
        with open(full, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _contains_position(line_no, col, start_line, start_col, end_line, end_col):
    # type: (int, int, int, int, int, int) -> bool
    if line_no < start_line or line_no > end_line:
        return False
    if line_no == start_line and col < start_col:
        return False
    if line_no == end_line and col >= end_col:
        return False
    return True


def _callees_for_defs(docs, defs, static_index):
    # type: (List[Dict], List[Dict], object) -> List[Dict]
    docs_by_path = {}
    for doc in docs:
        docs_by_path[_path(doc)] = doc
    rows = []
    call_cache = {}
    for definition in defs:
        doc = docs_by_path.get(definition["path"])
        if doc is None:
            continue
        start, end = _function_bounds(definition, doc, static_index)
        for occ in doc.get("occurrences") or []:
            symbol = str(occ.get("symbol") or "")
            line_no = _line(occ)
            if line_no < start or line_no > end:
                continue
            if _roles(occ) & DEFINITION_ROLE:
                continue
            if not _is_callable_symbol(symbol):
                continue
            if not _is_call_occurrence(doc, occ, call_cache):
                continue
            rows.append(_row(doc, occ, target_symbol=definition["symbol"]))
    return _dedupe_rows(rows)


def _function_bounds(definition, doc, static_index):
    # type: (Dict, Dict, object) -> (int, int)
    path = definition["path"]
    line_no = int(definition["line"])
    for key in getattr(static_index, "functions", {}):
        info = static_index.info(key)
        if info and info.get("rel") == path and int(info.get("line") or 0) == line_no:
            return int(info["line"]), int(info.get("end") or info["line"])
    occ = _find_definition_occurrence(doc, definition)
    enclosing = (occ or {}).get("enclosing_range") or (occ or {}).get("enclosingRange")
    if enclosing and len(enclosing) >= 3:
        return int(enclosing[0]) + 1, int(enclosing[2]) + 1
    return line_no, line_no


def _find_definition_occurrence(doc, definition):
    # type: (Dict, Dict) -> Optional[Dict]
    wanted_range = definition.get("range") or []
    wanted_symbol = definition.get("symbol")
    for occ in doc.get("occurrences") or []:
        if occ.get("symbol") == wanted_symbol and list(occ.get("range") or []) == wanted_range:
            return occ
    return None


def _dedupe_rows(rows):
    # type: (List[Dict]) -> List[Dict]
    seen = set()
    result = []
    for row in rows:
        key = (row["path"], row["line"], row.get("symbol"), _range_key(row))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    result.sort(key=lambda item: (item["path"], item["line"], item.get("display") or ""))
    return result


def display_symbol(symbol):
    # type: (str) -> str
    match = re.search(r"`([^`]+)`/(.+)$", symbol)
    if match:
        return f"{match.group(1)}/{match.group(2)}".rstrip(".")
    parts = symbol.split()
    tail = parts[-1] if parts else symbol
    return tail.rstrip(".")
