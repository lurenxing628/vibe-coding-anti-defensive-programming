"""产物新鲜度:检测静态图快照是否落后于源码,必要时重建。

重建调既有 callgraph_extract.py(硬依赖 networkx,优先用 .venv 的 python 跑);
产物目录由调用方传入(cli 用 static_index.callgraph_dir 解析一次的绝对路径),
重建时经 CHECKUP_CALLGRAPH 把同一绝对路径交给子进程,读取与重建落在同一目录。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional, Set, Tuple

from .static_index import FUNCTIONS_FILE

_FIRST_PARTY = ("src", "core", "web", "data", "app", "tools", "scripts", "desktop")
_EXCLUDE = {
    ".git", "__pycache__", ".venv", "venv", "node_modules", "vendor",
    "backups", "evidence", ".ruff_cache", ".pytest_cache", "dist", "build",
}


def snapshot_mtime(graph_dir: str) -> Optional[float]:
    functions_json = os.path.join(graph_dir, FUNCTIONS_FILE)
    if not os.path.exists(functions_json):
        return None
    return os.path.getmtime(functions_json)


def _scan_sources(repo_root: str) -> Tuple[float, Set[str]]:
    newest = 0.0
    current_files: Set[str] = set()
    for fn in os.listdir(repo_root):
        if not fn.endswith(".py"):
            continue
        fp = os.path.join(repo_root, fn)
        if not os.path.isfile(fp):
            continue
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        current_files.add(fn)
        if mtime > newest:
            newest = mtime
    for root in _FIRST_PARTY:
        base = os.path.join(repo_root, root)
        if not os.path.isdir(base):
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(current, fn)
                try:
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                rel = os.path.relpath(fp, repo_root).replace("\\", "/")
                current_files.add(rel)
                if mtime > newest:
                    newest = mtime
    return newest, current_files


def is_stale(repo_root: str, graph_dir: str) -> bool:
    snap = snapshot_mtime(graph_dir)
    if snap is None:
        return True
    newest, current_files = _scan_sources(repo_root)
    if newest > snap:
        return True
    try:
        with open(os.path.join(graph_dir, FUNCTIONS_FILE), encoding="utf-8") as fh:
            functions = json.load(fh)
    except (OSError, ValueError):
        return True
    if not isinstance(functions, dict):
        return True
    for info in functions.values():
        if not isinstance(info, dict) or info.get("rel") not in current_files:
            return True
    return False


def rebuild(repo_root: str, graph_dir: str) -> Optional[str]:
    """调 callgraph_extract 把产物重建到绝对路径 graph_dir。

    成功返回 None;失败返回去掉首尾空白的错误文本(子进程没有输出时为空串),交给 rebuild_error_hint。
    """
    extract = os.path.join(repo_root, "tools", "callgraph", "callgraph_extract.py")
    venv_py = os.path.join(repo_root, ".venv", "bin", "python")
    interpreter = venv_py if os.path.exists(venv_py) else sys.executable
    env = dict(os.environ)
    env["CHECKUP_CALLGRAPH"] = graph_dir
    try:
        proc = subprocess.run(
            [interpreter, extract], cwd=repo_root, env=env,
            capture_output=True, text=True)
    except OSError as exc:
        return str(exc).strip()
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout).strip()
    return None


def rebuild_error_hint(error: str) -> str:
    """把 rebuild 返回的失败文本变成 CLI 打印的可操作提示。"""
    if not error:
        return "重建静态图失败"
    if "networkx" in error.lower():
        return "重建静态图失败:缺少 networkx;请先修复仓库 .venv 里的 networkx 后重试。"
    return f"重建静态图失败:{error.splitlines()[0]}"


def snapshot_label(graph_dir: str) -> str:
    snap = snapshot_mtime(graph_dir)
    if snap is None:
        return "(无快照)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(snap))
