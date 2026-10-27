"""死代码使用图的数据结构。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class FunctionRecord:
    qual: str
    rel: str
    line: int
    end: int
    cls: Optional[str]
    name: str


@dataclass(frozen=True)
class UsageEvidence:
    target: str
    source_rel: str
    line: int
    kind: str
    detail: str


def module_name_from_rel(rel: str) -> str:
    path = rel[:-3] if rel.endswith(".py") else rel
    parts = path.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(part for part in parts if part)


def records_from_functions(functions: Dict[str, Dict]) -> Dict[str, FunctionRecord]:
    records = {}
    for qual, info in functions.items():
        records[qual] = FunctionRecord(
            qual=qual,
            rel=str(info["rel"]),
            line=int(info.get("line") or 0),
            end=int(info.get("end") or info.get("line") or 0),
            cls=info.get("cls"),
            name=str(info["name"]),
        )
    return records


def dedupe_evidence(items: Iterable[UsageEvidence]) -> List[UsageEvidence]:
    seen = set()
    result = []
    for item in items:
        key = (item.target, item.source_rel, item.line, item.kind, item.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    result.sort(key=lambda ev: (ev.target, ev.source_rel, ev.line, ev.kind, ev.detail))
    return result


def rel_join(root: str, rel: str) -> str:
    return os.path.join(root, rel.replace("/", os.sep))
