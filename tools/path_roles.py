"""仓库 Python 源码的路径角色：生产、测试、宿主工具三种，全仓只在这里定义一次。

扫描器按角色判断“谁的引用算数”：生产与宿主工具代码里的引用说明函数在用，
测试代码（含 `tools/host_tests/` 宿主测试）里的引用只说明它被测试使用。

根的写法有三种：目录（匹配其下全部文件）、根目录下的单个文件、根目录下的通配（只匹配仓库根下的文件，
判定见 `matches_root_file`，门禁范围的 `./` 写法共用它）。
同一路径按 `_ROLE_ROOTS` 的顺序取第一个命中的根：`tools/host_tests` 排在 `tools` 前归测试，
`app.py` 排在 `*.py` 前归生产。不属于任何根的路径没有角色，`role_of` 直接报错。
"""

from __future__ import annotations

import fnmatch
import os
from typing import Iterable, List, Sequence, Tuple

PRODUCTION = "production"
TEST = "test"
TOOL = "tool"
ROLES = (PRODUCTION, TEST, TOOL)

_ROLE_ROOTS: Tuple[Tuple[str, str], ...] = (
    ("tests", TEST),
    ("test", TEST),
    ("tools/host_tests", TEST),
    ("src", PRODUCTION),
    ("core", PRODUCTION),
    ("data", PRODUCTION),
    ("web", PRODUCTION),
    ("app", PRODUCTION),
    ("plugins", PRODUCTION),
    ("desktop", PRODUCTION),
    ("app.py", PRODUCTION),
    ("main.py", PRODUCTION),
    ("skills", TOOL),
    ("tools", TOOL),
    ("scripts", TOOL),
    ("*.py", TOOL),
)
SKIP_DIRS = frozenset({"__pycache__", "node_modules", "vendor", ".venv"})
#: 门禁范围里只匹配仓库根下文件的前缀（如 `./*.json`），由 `tools.test_registry.scope_matches_path` 解释。
ROOT_FILE_SCOPE_PREFIX = "./"


def _is_glob(root: str) -> bool:
    return any(char in root for char in "*?[")


def matches_root_file(rel_path: str, name_pattern: str) -> bool:
    """仓库相对路径（`/` 分隔）是仓库根下的文件，且文件名匹配通配（区分大小写）。"""
    return "/" not in rel_path and fnmatch.fnmatchcase(rel_path, name_pattern)


def _under_root(rel_path: str, root: str) -> bool:
    if _is_glob(root):
        return matches_root_file(rel_path, root)
    return rel_path == root or rel_path.startswith(root + "/")


def role_of(rel_path: str) -> str:
    """返回仓库相对路径（`/` 分隔）的角色。"""
    for root, role in _ROLE_ROOTS:
        if _under_root(rel_path, root):
            return role
    raise ValueError(f"路径不属于任何角色：{rel_path}")


def role_roots(roles: Iterable[str]) -> Tuple[str, ...]:
    wanted = set(roles)
    return tuple(root for root, role in _ROLE_ROOTS if role in wanted)


def _python_file_scope(root: str) -> str:
    if _is_glob(root):
        return ROOT_FILE_SCOPE_PREFIX + root
    return root if root.endswith(".py") else f"{root}/**/*.py"


def python_file_scopes(roots: Sequence[str]) -> Tuple[str, ...]:
    """把扫描根换成门禁范围：目录根取其下全部 `.py`，单个文件原样，根目录通配写成只匹配仓库根下文件的 `./` 范围。"""
    return tuple(_python_file_scope(root) for root in roots)


def iter_files(repo_root: str, roots: Sequence[str], suffixes: Tuple[str, ...]) -> List[str]:
    """列出各根下后缀匹配的文件（仓库相对路径、`/` 分隔、去重排序），跳过依赖与缓存目录。"""
    files: List[str] = []
    for root in roots:
        if _is_glob(root):
            files.extend(
                name for name in os.listdir(repo_root)
                if matches_root_file(name, root) and name.endswith(suffixes)
                and os.path.isfile(os.path.join(repo_root, name))
            )
            continue
        base = os.path.join(repo_root, root)
        if os.path.isfile(base):
            if root.endswith(suffixes):
                files.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
            files.extend(
                os.path.relpath(os.path.join(dirpath, name), repo_root).replace("\\", "/")
                for name in filenames
                if name.endswith(suffixes)
            )
    return sorted(set(files))


def iter_role_files(repo_root: str, roles: Sequence[str], suffixes: Tuple[str, ...]) -> List[str]:
    """列出属于指定角色的文件；目录根里归别的角色的子树（如 `tools/` 下的宿主测试）不计入。"""
    wanted = set(roles)
    return [path for path in iter_files(repo_root, role_roots(wanted), suffixes) if role_of(path) in wanted]
