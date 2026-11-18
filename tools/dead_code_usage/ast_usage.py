"""死代码扫描用的使用证据：公开引用索引里指向调用图函数的引用，去掉函数对自身的引用（递归）。"""
from __future__ import annotations

from typing import Dict, List, Sequence

from .model import CodeRoles, FunctionRecord, UsageEvidence
from .reference_index import build_reference_index


def collect_ast_usage(repo_root: str, records: Dict[str, FunctionRecord], rels: Sequence[str],
                      roles: CodeRoles) -> Dict[str, List[UsageEvidence]]:
    """解析 rels 列出的文件（仓库相对路径，由调用方按路径角色给出），收集对 records 中函数的使用证据。

    证据的 source_rel 是引用所在文件，origin 是给出它的代码角色（见 ``Reference.origin``），调用方按 origin 分栏。
    """
    index = build_reference_index(repo_root, records, rels, roles)
    evidence: Dict[str, List[UsageEvidence]] = {}
    for target, references in index.references.items():
        if target not in records:
            continue
        items = [
            UsageEvidence(target, reference.source_rel, reference.line, reference.kind, reference.source_qual, reference.origin)
            for reference in references
            if reference.source_qual != target
        ]
        if items:
            evidence[target] = items
    return evidence
