from __future__ import annotations

import os


def resolve_repo_root() -> str:
    """解析 tools/ 所属仓库根目录。"""
    return os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))


def real_repo_root(repo_root: str) -> str:
    return os.path.realpath(os.path.abspath(repo_root))


def is_within_repo(abs_path: str, repo_root: str) -> bool:
    root = os.path.normcase(real_repo_root(repo_root))
    real_path = os.path.normcase(os.path.realpath(os.path.abspath(abs_path)))
    return real_path == root or real_path.startswith(os.path.join(root, ""))


def resolve_repo_path(repo_root: str, path: str, *, description: str = "path") -> str:
    root = real_repo_root(repo_root)
    raw_path = str(path or "").replace("\\", "/")
    if not raw_path:
        raise ValueError(f"{description} is empty")
    if os.path.isabs(raw_path):
        abs_path = os.path.realpath(raw_path)
    else:
        abs_path = os.path.realpath(os.path.join(root, raw_path.replace("/", os.sep)))
    if not is_within_repo(abs_path, root):
        raise ValueError(f"{description} escapes repo root: {raw_path}")
    return abs_path
