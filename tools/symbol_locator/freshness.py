"""产物新鲜度:检测静态图快照是否落后于源码,必要时重建。

重建调既有 callgraph_extract.py(硬依赖 networkx,优先用 .venv 的 python 跑);
产物目录跟随 CHECKUP_CALLGRAPH(与 static_index、callgraph_extract 同源)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

from .static_index import FUNCTIONS_JSON, REPO_ROOT

_FIRST_PARTY = ("src", "core", "web", "data", "app", "tools", "scripts", "plugins", "desktop")
_EXTRACT = os.path.join(REPO_ROOT, "tools", "callgraph", "callgraph_extract.py")
_VENV_PY = os.path.join(REPO_ROOT, ".venv", "bin", "python")
_LAST_REBUILD_ERROR = None


def snapshot_mtime():
    # type: () -> Optional[float]
    if not os.path.exists(FUNCTIONS_JSON):
        return None
    return os.path.getmtime(FUNCTIONS_JSON)


def latest_source_mtime():
    # type: () -> float
    """FIRST_PARTY 根下所有 .py 的最新 mtime(扫描范围与 callgraph_extract 对齐)。"""
    newest = 0.0
    for root in _FIRST_PARTY:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                try:
                    mtime = os.path.getmtime(os.path.join(current, fn))
                except OSError:
                    continue
                if mtime > newest:
                    newest = mtime
    return newest


def is_stale():
    # type: () -> bool
    snap = snapshot_mtime()
    if snap is None:
        return True
    return latest_source_mtime() > snap


def rebuild():
    # type: () -> bool
    """调 callgraph_extract 重建产物(继承 CHECKUP_CALLGRAPH);成功返回 True。"""
    global _LAST_REBUILD_ERROR
    _LAST_REBUILD_ERROR = None
    interpreter = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable
    try:
        proc = subprocess.run(
            [interpreter, _EXTRACT], cwd=REPO_ROOT, check=True, env=dict(os.environ),
            capture_output=True, text=True)
        if proc.returncode != 0:
            _LAST_REBUILD_ERROR = (proc.stderr or proc.stdout or "").strip()
            return False
        return True
    except subprocess.CalledProcessError as exc:
        _LAST_REBUILD_ERROR = (exc.stderr or exc.stdout or str(exc)).strip()
        return False
    except Exception as exc:
        _LAST_REBUILD_ERROR = str(exc)
        return False


def rebuild_error_hint():
    # type: () -> Optional[str]
    """给 CLI 打印重建失败的可操作提示。"""
    if not _LAST_REBUILD_ERROR:
        return None
    lower = _LAST_REBUILD_ERROR.lower()
    if "networkx" in lower:
        return "重建静态图失败:缺少 networkx;请先修复仓库 .venv 里的 networkx 后重试。"
    first_line = _LAST_REBUILD_ERROR.splitlines()[0].strip()
    if first_line:
        return f"重建静态图失败:{first_line}"
    return None


def snapshot_label():
    # type: () -> str
    snap = snapshot_mtime()
    if snap is None:
        return "(无快照)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(snap))
