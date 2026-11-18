from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

SCHEMA_VERSION = 4
SCOPE_PRODUCTION = "production"
SCOPE_WITH_TESTS = "production-and-tests"
BASELINE_NOTE = (
    "循环依赖基线。正式门禁同时锁定 scope/roots/文件加载语义；硬加载期目录环、硬加载期文件环、"
    "延迟目录环（运行时图相对硬加载期图新增）与运行时文件环四类都按圈成员超集匹配；圈内规范化有向模块边"
    "只锁该类环的锁定类别：硬加载期两类锁全部圈内边（都是顶层硬导入），延迟/运行时两类只锁函数体内（lazy）"
    "与条件（cond）导入边，函数体内导入只推迟加载，闭合的延迟环同样计入；圈内新增的普通顶层导入不锁，"
    "若它闭合成环由硬加载期两类拦下。未解析动态导入按 file/scope/context/loader_kind/expression/call_ast "
    "稳定语义身份与 count 锁定（物理行号仅用于定位展示，同名/匿名作用域按名称路径合并计数）；"
    "删边、消圈或 SCC 缩小允许，新增债务阻断；--update-baseline 默认拒绝增长，人工核对后加 --allow-growth。"
)
UNRESOLVED_IDENTITY_FIELDS = ("file", "scope", "context", "loader_kind", "expression", "call_ast")
CycleMap = Dict[str, Set[str]]
CycleEdge = Tuple[str, str]


class CycleCategory(NamedTuple):
    name: str
    result_key: str
    label: str
    locked_kinds: Tuple[str, ...]


# 四类环共用同一套成员超集 + 圈内锁定边比较；name 是基线字段，result_key 是扫描结果字段，
# locked_kinds 是该类环圈内要锁定的 import 类别。延迟/运行时两类是靠 lazy/cond 边闭合的，只锁这两类边：
# 圈内新增普通顶层导入若闭合成环由硬加载期两类拦下，新成员由成员超集拦下，正常分层导入不必刷新基线。
CYCLE_CATEGORIES = (
    CycleCategory("hard_dir_cycles", "hard_dir_cycles", "硬加载期目录环", ("hard",)),
    CycleCategory("hard_file_cycles", "hard_file_cycle_records", "硬加载期文件环", ("hard",)),
    CycleCategory("delayed_dir_cycles", "delayed_dir_cycles", "延迟目录环", ("cond", "lazy")),
    CycleCategory("runtime_file_cycles", "runtime_file_cycles", "运行时文件环", ("cond", "lazy")),
)


class UnresolvedDynamicKey(NamedTuple):
    file: str
    scope: str
    context: str
    loader_kind: str
    expression: str
    call_ast: str


class CycleBaselineError(ValueError):
    """基线存在但无法可信读取。"""


@dataclass
class CycleBaseline:
    scope: str
    scan_roots: Tuple[str, ...]
    file_cycle_semantics: str
    cycles: Dict[str, CycleMap]
    unresolved_dynamic_imports: Dict[UnresolvedDynamicKey, int]


@dataclass
class CycleBaselineComparison:
    baseline_missing: bool
    new_cycles: Dict[str, Set[str]]
    new_edges: Dict[str, Set[CycleEdge]]
    new_unresolved_dynamic_imports: Set[str]

    @property
    def has_new(self) -> bool:
        return (
            any(self.new_cycles.values())
            or any(self.new_edges.values())
            or bool(self.new_unresolved_dynamic_imports)
        )

    @property
    def clean(self) -> bool:
        return not self.baseline_missing and not self.has_new


def baseline_scope(include_tests: bool) -> str:
    return SCOPE_WITH_TESTS if include_tests else SCOPE_PRODUCTION


def default_baseline_path(repo_root: str, include_tests: bool = False) -> str:
    filename = "import_cycles_with_tests_baseline.json" if include_tests else "import_cycles_production_baseline.json"
    return os.path.join(repo_root, ".checkup", filename)


def cycle_signature(members: Sequence[str]) -> str:
    return "|".join(sorted(members))


def edge_signature(source: str, target: str) -> str:
    return f"{source} -> {target}"


def _locked_edges(record: dict, category: CycleCategory) -> List[dict]:
    return [edge for edge in record["edges"] if edge["kind"] in category.locked_kinds]


def _cycle_map(records: Sequence[dict], category: CycleCategory) -> CycleMap:
    return {
        cycle_signature(record["members"]): {
            edge_signature(edge["source"], edge["target"]) for edge in _locked_edges(record, category)
        }
        for record in records
    }


def current_cycle_maps(result: dict) -> Dict[str, CycleMap]:
    return {category.name: _cycle_map(result[category.result_key], category) for category in CYCLE_CATEGORIES}


def cycle_count_summary(result: dict) -> str:
    return " / ".join(f"{category.label} {len(result[category.result_key])} 个" for category in CYCLE_CATEGORIES)


def _unresolved_key(row: dict, *, error_prefix: str) -> UnresolvedDynamicKey:
    values: List[str] = []
    for field in UNRESOLVED_IDENTITY_FIELDS:
        val = row.get(field)
        if not isinstance(val, str) or not val.strip():
            raise CycleBaselineError(f"{error_prefix}.{field} 非法")
        values.append(val)
    return UnresolvedDynamicKey(*values)


def _row_key(row: dict) -> UnresolvedDynamicKey:
    return UnresolvedDynamicKey(
        row["file"],
        row["scope"],
        row["context"],
        row["loader_kind"],
        row["expression"],
        row["call_ast"],
    )


def unresolved_dynamic_counts(result: dict) -> Dict[UnresolvedDynamicKey, int]:
    counts: Dict[UnresolvedDynamicKey, int] = {}
    for row in result["unresolved_dynamic_imports"]:
        key = _row_key(row)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _load_unresolved_rows(value: object, *, path: str) -> Dict[UnresolvedDynamicKey, int]:
    if not isinstance(value, list):
        raise CycleBaselineError(f"循环依赖基线 {path} 的 unresolved_dynamic_imports 非法")
    counts: Dict[UnresolvedDynamicKey, int] = {}
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise CycleBaselineError(f"循环依赖基线 {path} 的 unresolved_dynamic_imports[{index}] 必须是对象")
        key = _unresolved_key(row, error_prefix=f"循环依赖基线 {path} 的 unresolved_dynamic_imports[{index}]")
        count = row.get("count")
        if type(count) is not int or count <= 0:
            raise CycleBaselineError(f"循环依赖基线 {path} 的 unresolved_dynamic_imports[{index}].count 非法")
        if key in counts:
            raise CycleBaselineError(f"循环依赖基线 {path} 的 unresolved_dynamic_imports 存在重复身份键：{key}")
        counts[key] = count
    return counts


def _load_cycle_rows(value: object, *, field: str, path: str) -> CycleMap:
    if not isinstance(value, list):
        raise CycleBaselineError(f"基线 {path} 的 {field} 必须是数组；请人工核对后受控重建 v{SCHEMA_VERSION} 基线")
    cycles: CycleMap = {}
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            raise CycleBaselineError(f"基线 {path} 的 {field}[{index}] 必须是对象")
        members = row.get("members")
        edges = row.get("edges")
        if not isinstance(members, list) or not members or not all(isinstance(item, str) and item for item in members):
            raise CycleBaselineError(f"基线 {path} 的 {field}[{index}].members 非法")
        if not isinstance(edges, list) or not all(isinstance(item, str) and " -> " in item for item in edges):
            raise CycleBaselineError(f"基线 {path} 的 {field}[{index}].edges 非法")
        signature = cycle_signature(members)
        if signature in cycles:
            raise CycleBaselineError(f"基线 {path} 的 {field} 存在重复循环成员：{signature}")
        cycles[signature] = set(edges)
    return cycles


def load_baseline(path: str, *, expected_scope: str) -> Optional[CycleBaseline]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CycleBaselineError(f"无法读取循环依赖基线 {path}：{exc}；请修复文件后重试") from exc
    if not isinstance(data, dict):
        raise CycleBaselineError(f"循环依赖基线 {path} 顶层必须是对象")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise CycleBaselineError(
            f"循环依赖基线 {path} 的 schema_version={version!r} 不受支持；当前只支持 {SCHEMA_VERSION}，"
            "请先查看扫描差异，再受控重建基线"
        )
    scope = str(data.get("scope") or "").strip()
    if scope != expected_scope:
        raise CycleBaselineError(
            f"循环依赖基线 {path} 的 scope={scope!r}，当前扫描要求 {expected_scope!r}；生产与含测试基线不可混用"
        )
    scan_roots = data.get("scan_roots")
    if not isinstance(scan_roots, list) or not all(isinstance(item, str) and item for item in scan_roots):
        raise CycleBaselineError(f"循环依赖基线 {path} 的 scan_roots 非法")
    file_cycle_semantics = str(data.get("file_cycle_semantics") or "").strip()
    if not file_cycle_semantics:
        raise CycleBaselineError(f"循环依赖基线 {path} 缺少 file_cycle_semantics")
    unresolved = _load_unresolved_rows(data.get("unresolved_dynamic_imports"), path=path)
    return CycleBaseline(
        scope=scope,
        scan_roots=tuple(scan_roots),
        file_cycle_semantics=file_cycle_semantics,
        cycles={
            category.name: _load_cycle_rows(data.get(category.name), field=category.name, path=path)
            for category in CYCLE_CATEGORIES
        },
        unresolved_dynamic_imports=unresolved,
    )


def _payload_rows(cycles: CycleMap) -> List[dict]:
    return [
        {"members": signature.split("|"), "edges": sorted(edges)}
        for signature, edges in sorted(cycles.items())
    ]


def _unresolved_payload_rows(counts: Dict[UnresolvedDynamicKey, int]) -> List[dict]:
    return [
        {
            "file": key.file,
            "scope": key.scope,
            "context": key.context,
            "loader_kind": key.loader_kind,
            "expression": key.expression,
            "call_ast": key.call_ast,
            "count": count,
        }
        for key, count in sorted(counts.items())
    ]


def write_baseline(path: str, result: dict, *, scope: str) -> None:
    cycles = current_cycle_maps(result)
    payload = {
        "note": BASELINE_NOTE,
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "scan_roots": result["scan_roots"],
        "file_cycle_semantics": result["file_cycle_semantics"],
        **{name: _payload_rows(rows) for name, rows in cycles.items()},
        "unresolved_dynamic_imports": _unresolved_payload_rows(unresolved_dynamic_counts(result)),
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def _baseline_supersets(signature: str, baseline: CycleMap) -> List[str]:
    members = set(signature.split("|"))
    return [
        baseline_signature
        for baseline_signature in baseline
        if members <= set(baseline_signature.split("|"))
    ]


def _new_cycle_signatures(current: CycleMap, baseline: CycleMap) -> Set[str]:
    return {
        signature
        for signature in current
        if not _baseline_supersets(signature, baseline)
    }


def _new_edges(current: CycleMap, baseline: CycleMap) -> Set[CycleEdge]:
    out: Set[CycleEdge] = set()
    for signature, edges in current.items():
        matches = _baseline_supersets(signature, baseline)
        if not matches:
            continue
        accepted_edges = set().union(*(baseline[match] for match in matches))
        out.update((signature, edge) for edge in edges - accepted_edges)
    return out


def _new_unresolved_dynamic_imports(
    result: dict,
    baseline_counts: Dict[UnresolvedDynamicKey, int],
) -> Set[str]:
    current_counts: Dict[UnresolvedDynamicKey, int] = {}
    lines_by_key: Dict[UnresolvedDynamicKey, List[int]] = {}
    for row in result["unresolved_dynamic_imports"]:
        key = _row_key(row)
        current_counts[key] = current_counts.get(key, 0) + 1
        lines_by_key.setdefault(key, []).append(row["line"])
    out: Set[str] = set()
    for key, current_count in current_counts.items():
        baseline_count = baseline_counts.get(key, 0)
        if current_count > baseline_count:
            lines = ",".join(str(line) for line in lines_by_key[key])
            out.add(
                f"{key.file}:{lines} [{key.context}] scope={key.scope} loader={key.loader_kind} "
                f"{key.expression} ast={key.call_ast} (count {baseline_count} -> {current_count})"
            )
    return out


def compare_with_baseline(path: str, result: dict, *, expected_scope: str) -> CycleBaselineComparison:
    baseline = load_baseline(path, expected_scope=expected_scope)
    if baseline is None:
        return CycleBaselineComparison(True, {}, {}, set())
    current_roots = tuple(result["scan_roots"])
    if current_roots != baseline.scan_roots:
        raise CycleBaselineError(
            f"循环依赖基线 {path} 的 scan_roots={list(baseline.scan_roots)!r}，"
            f"当前扫描 roots={list(current_roots)!r}；扫描范围变化必须人工核对后受控重建基线"
        )
    if result["file_cycle_semantics"] != baseline.file_cycle_semantics:
        raise CycleBaselineError(
            f"循环依赖基线 {path} 的 file_cycle_semantics 与当前扫描器不一致；请人工核对后受控重建基线"
        )
    current = current_cycle_maps(result)
    return CycleBaselineComparison(
        baseline_missing=False,
        new_cycles={name: _new_cycle_signatures(cycles, baseline.cycles[name]) for name, cycles in current.items()},
        new_edges={name: _new_edges(cycles, baseline.cycles[name]) for name, cycles in current.items()},
        new_unresolved_dynamic_imports=_new_unresolved_dynamic_imports(
            result, baseline.unresolved_dynamic_imports
        ),
    )


def print_new_cycles(result: dict, comparison: CycleBaselineComparison) -> None:
    counts = "；".join(
        f"{category.label} {len(comparison.new_cycles[category.name])} 个、"
        f"圈内锁定边 {len(comparison.new_edges[category.name])} 条"
        for category in CYCLE_CATEGORIES
    )
    print(
        f"⚠️ [import-cycles] 检出基线外新增循环依赖债务：{counts}；"
        f"未解析动态导入 {len(comparison.new_unresolved_dynamic_imports)} 个",
        flush=True,
    )
    for category in CYCLE_CATEGORIES:
        by_sig = {cycle_signature(record["members"]): record for record in result[category.result_key]}
        for signature in sorted(comparison.new_cycles[category.name]):
            record = by_sig[signature]
            print(f"  + [{category.label}] {' ⇄ '.join(record['members'])}", flush=True)
            for edge in _locked_edges(record, category)[:4]:
                print(f"        {edge['src']}:{edge['line']} -> {edge['target']} [{edge['kind']}]", flush=True)
        for signature, edge in sorted(comparison.new_edges[category.name]):
            print(
                f"  + [{category.label}圈内新增锁定边] {edge}  (圈：{' ⇄ '.join(signature.split('|'))})",
                flush=True,
            )
    for unresolved in sorted(comparison.new_unresolved_dynamic_imports):
        print(f"  + [新增未解析动态导入] {unresolved}", flush=True)
    print(
        "  处理：先断开新增依赖（改成函数体内导入只推迟加载，不算断开）；"
        "确需接受时，必须先人工核对差异，再用 --update-baseline --allow-growth 受控刷新。",
        flush=True,
    )
