"""jedi 实时消歧:在调用点 goto 出精确定义(lazy import jedi)。

jedi 跑在工具解释器(系统 python3.14),只读 Py3.8 源码;jedi 不可用时返回 None,由上层降级。
顶层不 import jedi——否则 .venv(3.8,无 jedi)导入本包即崩。
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from .static_index import REPO_ROOT

# (rel, line, full_name);rel 可能为 None(指向 stdlib/三方,无项目内路径)
Hit = Tuple[Optional[str], int, Optional[str]]


def available():
    # type: () -> bool
    try:
        import jedi  # noqa: F401
        return True
    except Exception:
        return False


def resolve_at(file_rel, line, symbol, repo_root=None):
    # type: (str, int, str, Optional[str]) -> Optional[List[Hit]]
    """在 file_rel:line 行的 symbol 调用点 goto。

    返回命中列表 [(rel, line, full_name)];jedi 缺失或无法定位调用点时返回 None(上层降级)。
    """
    try:
        import jedi
    except Exception:
        return None
    root = repo_root or REPO_ROOT
    path = os.path.join(root, file_rel)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        code = fh.read()
    rows = code.splitlines()
    if line < 1 or line > len(rows):
        return None
    col = rows[line - 1].find(symbol)
    if col < 0:
        return None
    project = jedi.Project(root)
    script = jedi.Script(code, path=path, project=project)
    hits = []  # type: List[Hit]
    for d in script.goto(line, col + 1, follow_imports=True):
        mp = d.module_path
        rel = os.path.relpath(str(mp), root) if mp else None
        hits.append((rel, d.line, d.full_name))
    return hits
