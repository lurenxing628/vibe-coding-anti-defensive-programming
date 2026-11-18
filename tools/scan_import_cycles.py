#!/usr/bin/env python3
"""包级 / 文件级循环依赖扫描器(import-cycle scanner)。

补 checkup 函数调用图的盲区：其 cycle_count 是确信边上长度 2-8、最多 200 条的
simple-cycle 记录数，不是 import SCC。本工具按目录(包)与文件(模块)两种粒度，用 Tarjan 求强连通
分量,定位加载期可能 ImportError 的硬环,以及靠函数体内导入推迟、运行时仍闭合的延迟环。

import 四分类(决定一条 import 边进哪张图):
  hard      无条件顶层 import(模块加载期必执行)—— 真正可能 ImportError 的环
  cond      顶层 try / 非 TYPE_CHECKING 的 if 块内(条件加载)
  lazy      函数体内(只推迟加载,不拆环;闭合的延迟环同样计入基线、只减不增)
  typeonly  TYPE_CHECKING 块体(运行时不执行,不计入耦合)

四类环分开报,正式门禁(--fail-on-new-cycle)按 scope 基线全部只减不增(成员按超集匹配):
  ① 硬加载期目录环(只 hard 边)                    锁全部圈内边
  ② 硬加载期文件环(hard 显式边 + 父包初始化边)     锁全部圈内边
  ③ 延迟目录环(运行时图相对硬加载期图新增的目录环) 只锁圈内 lazy/cond 边
  ④ 运行时文件环(运行时图上的文件环)               只锁圈内 lazy/cond 边
  ③④ 是靠 lazy/cond 边闭合的;圈内新增普通顶层导入不锁,它若闭合成环由 ①② 拦下。

--update-baseline 默认拒绝增长(新增环 / 新增成员 / 圈内新增锁定边 / 未解析动态导入计数增加),
人工核对差异后加 --allow-growth 才写入。

退出码:
  0  正常(相对基线无新增,或未加正式门禁参数);基线已刷新
  1  --fail-on-new-cycle 检出基线外新增环 / 圈内锁定边 / 未解析动态导入;--fail-on-hard-cycle 检出硬加载期环;
     --update-baseline 未加 --allow-growth 时遇到增长
  2  工具自身错误、基线缺失或异常、正式模式下源码解析失败

口径要点(经 Codex 对抗核验修正,):
  - `from . import 兄弟模块` 不连"包根"假边:仅当确有 name 来自 __init__/包符号时才连包根。
  - TYPE_CHECKING 的 else 归运行时(此前整块跳过会漏 else 边)。
  - 动态 importlib.import_module("字面量") 按顶层/函数内定 hard/lazy。
  - 运行时图 = hard + cond + lazy(排除 typeonly,运行时不执行)。

扫描范围按 tools/path_roles.py 的路径角色：默认扫生产与宿主工具代码；测试角色(tests/ 与 tools/host_tests/)
只在 --include-tests 时纳入。
Py3.8 兼容、纯标准库。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from tools import import_cycle_graph as _graph
from tools.import_cycle_analysis import classify, dyn_imports, expand_parent_packages, resolve_targets
from tools.import_cycle_baseline import (
    CycleBaselineComparison,
    CycleBaselineError,
    baseline_scope,
    compare_with_baseline,
    cycle_count_summary,
    default_baseline_path,
    print_new_cycles,
    write_baseline,
)
from tools.path_roles import PRODUCTION, ROLES, TOOL, iter_role_files, role_roots
from tools.repo_paths import resolve_repo_root

PRODUCTION_SCOPE_ROLES = (PRODUCTION, TOOL)
WITH_TESTS_SCOPE_ROLES = ROLES
FILE_CYCLE_SEMANTICS = "explicit import edges plus implicit parent-package __init__ loading edges"


path_to_mod = _graph.module_from_path
cur_pkg_parts = _graph.current_package_parts
tdir = _graph.target_directory
render_text = _graph.render_text


_adj = _graph.adjacency
_merge = _graph.merge_edges
_dir_cycle_records = _graph.dir_cycle_records
_file_cycle_records = _graph.file_cycle_records
_delayed_dir_cycle_records = _graph.delayed_dir_cycle_records


def _index_modules(roles: Sequence[str], repo_root: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    file_to_mod: Dict[str, str] = {}
    mod_to_file: Dict[str, str] = {}
    for rel in iter_role_files(repo_root, roles, (".py",)):
        absolute = os.path.abspath(os.path.join(repo_root, rel))
        module = path_to_mod(rel)
        file_to_mod[absolute] = module
        mod_to_file[module] = absolute
    return file_to_mod, mod_to_file


def _add_edge(
    edges_file: dict,
    edges_dir: dict,
    *,
    kind: str,
    source_mod: str,
    target_mod: str,
    source_dir: str,
    rel_path: str,
    line: int,
    mod_to_file: Dict[str, str],
    repo_root: str,
    include_directory_edge: bool = True,
) -> None:
    if target_mod == source_mod:
        return
    edges_file[kind][(source_mod, target_mod)].append((rel_path, line))
    target_dir = tdir(target_mod, mod_to_file, repo_root)
    if include_directory_edge and target_dir is not None and target_dir != source_dir:
        edges_dir[kind][(source_dir, target_dir)].append((rel_path, line, target_mod))


def _resolved_dynamic_target(
    target: str,
    package: Optional[str],
    pkg: List[str],
    source_mod: str,
) -> Optional[str]:
    if not target.startswith("."):
        return target
    if package == "__package__":
        package_name = ".".join(pkg)
    elif package == "__name__":
        package_name = source_mod
    else:
        package_name = str(package or "").strip()
    if not package_name:
        return None
    try:
        return importlib.util.resolve_name(target, package_name)
    except (ImportError, ValueError):
        return None


def _collect_module_edges(
    ab: str,
    mod: str,
    tree: ast.AST,
    file_to_mod: Dict[str, str],
    mod_to_file: Dict[str, str],
    edges_file: dict,
    explicit_edges_file: dict,
    edges_dir: dict,
    unresolved_dynamic_imports: List[dict],
    parent_package_init_edges: List[dict],
    repo_root: str,
) -> None:
    pkg = cur_pkg_parts(ab, mod)
    rel = os.path.relpath(ab, repo_root).replace("\\", "/")
    source_dir = os.path.dirname(os.path.relpath(ab, repo_root)).replace("\\", "/") or "."
    for node, kind in classify(tree):
        direct_targets = resolve_targets(node, pkg, mod_to_file, include_parent_packages=False)
        for target_mod in expand_parent_packages(direct_targets, mod_to_file):
            parent_init = target_mod not in direct_targets
            _add_edge(
                edges_file,
                edges_dir,
                kind=kind,
                source_mod=mod,
                target_mod=target_mod,
                source_dir=source_dir,
                rel_path=rel,
                line=node.lineno,  # type: ignore[attr-defined]
                mod_to_file=mod_to_file,
                repo_root=repo_root,
                include_directory_edge=not parent_init,
            )
            if not parent_init:
                explicit_edges_file[kind][(mod, target_mod)].append((rel, node.lineno))  # type: ignore[attr-defined]
            if parent_init:
                parent_package_init_edges.append(
                    {
                        "source": mod,
                        "target": target_mod,
                        "file": rel,
                        "line": node.lineno,  # type: ignore[attr-defined]
                        "context": kind,
                    }
                )
    resolved_dynamic, unresolved_dynamic = dyn_imports(tree, source_path=ab, file_to_mod=file_to_mod)
    for dynamic_import in resolved_dynamic:
        raw_target, package = dynamic_import.target, dynamic_import.package
        kind, line = dynamic_import.context, dynamic_import.line
        target_mod = _resolved_dynamic_target(raw_target, package, pkg, mod)
        dynamic_targets = _dynamic_targets(dynamic_import, target_mod, mod_to_file)
        for dynamic_target in dynamic_targets:
            if dynamic_target not in mod_to_file:
                continue
            parent_init = dynamic_target != target_mod
            _add_edge(
                edges_file,
                edges_dir,
                kind=kind,
                source_mod=mod,
                target_mod=dynamic_target,
                source_dir=source_dir,
                rel_path=rel,
                line=line,
                mod_to_file=mod_to_file,
                repo_root=repo_root,
                include_directory_edge=not parent_init,
            )
            if not parent_init:
                explicit_edges_file[kind][(mod, dynamic_target)].append((rel, line))
            if parent_init:
                parent_package_init_edges.append(
                    {"source": mod, "target": dynamic_target, "file": rel, "line": line, "context": kind}
                )
        if isinstance(dynamic_import, _graph.ResolvedNameImport) and target_mod not in mod_to_file and raw_target.startswith("."):
            expr = f"relative target {raw_target!a} could not resolve in package {package!a}"
            unresolved_dynamic_imports.append(
                _graph.UnresolvedDynamicImport(
                    kind, line, dynamic_import.scope, dynamic_import.loader_kind, expr, dynamic_import.call_ast
                ).to_record(rel)
            )
    for item in unresolved_dynamic:
        unresolved_dynamic_imports.append(item.to_record(rel))


def _dynamic_targets(dynamic_import: tuple, target_mod: Optional[str], mod_to_file: Dict[str, str]) -> List[str]:
    # A file spec loads its source directly, not the source's package initializers.
    if isinstance(dynamic_import, _graph.ResolvedFileImport):
        return [dynamic_import.target]
    return expand_parent_packages([target_mod], mod_to_file) if target_mod else []


def scan(roles: Sequence[str], repo_root: str) -> dict:
    """扫描属于给定路径角色的 Python 文件,返回四类环 + 边计数 + 解析失败的结构化结果。"""
    file_to_mod, mod_to_file = _index_modules(roles, repo_root)
    edges_file = {k: defaultdict(list) for k in _graph.IMPORT_KINDS}
    explicit_edges_file = {k: defaultdict(list) for k in _graph.IMPORT_KINDS}
    edges_dir = {k: defaultdict(list) for k in _graph.IMPORT_KINDS}
    parse_errors: List[dict] = []
    unresolved_dynamic_imports: List[dict] = []
    parent_package_init_edges: List[dict] = []

    for ab, mod in file_to_mod.items():
        try:
            with open(ab, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append({"file": os.path.relpath(ab, repo_root), "error": str(exc)})
            continue
        _collect_module_edges(
            ab,
            mod,
            tree,
            file_to_mod,
            mod_to_file,
            edges_file,
            explicit_edges_file,
            edges_dir,
            unresolved_dynamic_imports,
            parent_package_init_edges,
            repo_root,
        )

    hard_dir = _merge(edges_dir, ("hard",))
    hard_file = _merge(edges_file, ("hard",))
    explicit_hard_file = _merge(explicit_edges_file, ("hard",))
    rt_dir = _merge(edges_dir, _graph.RUNTIME_KINDS)
    rt_file = _merge(edges_file, _graph.RUNTIME_KINDS)
    explicit_rt_file = _merge(explicit_edges_file, _graph.RUNTIME_KINDS)

    hard_dir_sccs = _graph.tarjan(_adj(hard_dir))
    hard_file_sccs = _graph.tarjan(_adj(hard_file))
    explicit_hard_file_sccs = _graph.tarjan(_adj(explicit_hard_file))
    rt_dir_sccs = _graph.tarjan(_adj(rt_dir))
    rt_file_sccs = _graph.tarjan(_adj(rt_file))
    explicit_rt_file_sccs = _graph.tarjan(_adj(explicit_rt_file))

    return {
        "module_count": len(file_to_mod),
        "scan_roots": list(role_roots(roles)),
        "file_cycle_semantics": FILE_CYCLE_SEMANTICS,
        "parse_errors": parse_errors,
        "edge_counts": {k: sum(len(v) for v in edges_file[k].values()) for k in _graph.IMPORT_KINDS},
        "hard_dir_cycles": _dir_cycle_records(hard_dir_sccs, hard_dir),
        "hard_file_cycles": [sorted(c) for c in sorted(hard_file_sccs, key=len, reverse=True)],
        "hard_file_cycle_records": _file_cycle_records(hard_file_sccs, hard_file),
        "explicit_hard_file_cycles": _file_cycle_records(explicit_hard_file_sccs, explicit_hard_file),
        "delayed_dir_cycles": _delayed_dir_cycle_records(rt_dir_sccs, hard_dir_sccs, rt_dir),
        "runtime_file_cycle_count": len(rt_file_sccs),
        "runtime_file_cycles": _file_cycle_records(rt_file_sccs, rt_file),
        "explicit_runtime_file_cycles": _file_cycle_records(explicit_rt_file_sccs, explicit_rt_file),
        "parent_package_init_edges": sorted(
            parent_package_init_edges,
            key=lambda item: (item["source"], item["target"], item["line"]),
        ),
        "unresolved_dynamic_imports": sorted(
            unresolved_dynamic_imports,
            key=lambda item: (item["file"], item["line"], item["expression"]),
        ),
    }


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="包级/文件级循环依赖扫描器")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON(供门禁/checkup 消费)")
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="把测试角色(tests/ 与 tools/host_tests/)也纳入扫描(默认只扫生产与宿主工具代码)",
    )
    parser.add_argument(
        "--fail-on-hard-cycle",
        action="store_true",
        help="检出任何硬加载期目录环/文件环即退出码 1(治理完成后用,严格禁环)",
    )
    parser.add_argument(
        "--fail-on-new-cycle",
        action="store_true",
        help="按 scope 匹配的 v4 基线阻断四类环的新增环、新增成员或圈内新增锁定边；基线/解析异常 fail closed",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="把当前四类环及圈内锁定边写入 scope 对应基线；默认拒绝任何增长",
    )
    parser.add_argument(
        "--allow-growth",
        action="store_true",
        help="与 --update-baseline 合用：人工核对差异后允许把增长写入基线",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="显式基线路径；默认按是否 --include-tests 选择生产或含测试基线",
    )
    parser.add_argument(
        "--quiet-when-clean",
        action="store_true",
        help="无需报告时完全不输出(clean 含义随模式:相对基线无新增 / 无任何硬环)",
    )
    parser.add_argument("--repo-root", default=resolve_repo_root(), help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.baseline is None:
        args.baseline = default_baseline_path(args.repo_root, include_tests=bool(args.include_tests))
    return args


def _error(message: str) -> None:
    print(f"[import-cycles] {message}", file=sys.stderr, flush=True)


def _load_scan_result(args: argparse.Namespace) -> Tuple[Optional[dict], int]:
    roles = WITH_TESTS_SCOPE_ROLES if args.include_tests else PRODUCTION_SCOPE_ROLES
    try:
        return scan(roles, args.repo_root), 0
    except Exception as exc:
        _error(f"扫描自身异常，无法产出可信结果：{exc}")
        return None, 2


def _print_parse_errors(result: dict) -> None:
    errors = result["parse_errors"]
    _error(f"源码解析失败 {len(errors)} 个，正式循环门禁已阻断；请先修复下列文件：")
    for row in errors[:20]:
        _error(f"  - {row['file']}: {row['error']}")
    if len(errors) > 20:
        _error(f"  - 另有 {len(errors) - 20} 个解析失败")


def _print_result(
    result: dict,
    args: argparse.Namespace,
    comparison: Optional[CycleBaselineComparison],
) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(render_text(result), flush=True)
    if not args.fail_on_new_cycle or comparison is None:
        return
    if comparison.has_new:
        print_new_cycles(result, comparison)
    else:
        print(f"[import-cycles] 相对基线无新增环或圈内锁定边(现存:{cycle_count_summary(result)})。", flush=True)


def _baseline_comparison(
    args: argparse.Namespace,
    result: dict,
    scope: str,
) -> Tuple[Optional[CycleBaselineComparison], int]:
    try:
        comparison = compare_with_baseline(args.baseline, result, expected_scope=scope)
    except CycleBaselineError as exc:
        _error(str(exc))
        return None, 2
    if comparison.baseline_missing:
        _error(
            f"正式循环门禁缺少 {scope!r} 基线：{args.baseline}；"
            "请先查看完整扫描差异，确认无新增债务后再受控执行 --update-baseline"
        )
        return None, 2
    return comparison, 0


def _update_baseline(args: argparse.Namespace, result: dict, scope: str) -> int:
    if not args.allow_growth:
        try:
            comparison = compare_with_baseline(args.baseline, result, expected_scope=scope)
        except CycleBaselineError as exc:
            _error(str(exc))
            return 2
        if comparison.has_new:
            print_new_cycles(result, comparison)
            _error("基线刷新被拒绝：存在上面列出的增长；请先断开新增依赖，或人工核对后加 --allow-growth")
            return 1
    try:
        write_baseline(args.baseline, result, scope=scope)
    except OSError as exc:
        _error(f"无法写入循环依赖基线 {args.baseline}：{exc}")
        return 2
    rel = os.path.relpath(args.baseline, args.repo_root)
    print(f"[import-cycles] {scope} 基线已刷新：{cycle_count_summary(result)} -> {rel}", flush=True)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    result, error_code = _load_scan_result(args)
    if result is None:
        return error_code

    formal_mode = bool(args.fail_on_new_cycle or args.fail_on_hard_cycle or args.update_baseline)
    if formal_mode and result["parse_errors"]:
        _print_result(result, args, None)
        _print_parse_errors(result)
        return 2

    scope = baseline_scope(bool(args.include_tests))
    if args.update_baseline:
        return _update_baseline(args, result, scope)

    has_hard = bool(result["hard_dir_cycles"]) or bool(result["hard_file_cycles"])
    comparison: Optional[CycleBaselineComparison] = None
    if args.fail_on_new_cycle:
        comparison, error_code = _baseline_comparison(args, result, scope)
        if comparison is None:
            _print_result(result, args, None)
            return error_code

    clean = comparison.clean if comparison is not None else not has_hard
    if args.quiet_when_clean and clean and not args.json:
        return 0

    _print_result(result, args, comparison)

    if args.fail_on_hard_cycle and has_hard:
        if not args.json:
            print(
                f"[import-cycles] ⚠️ 检出硬加载期环(目录 {len(result['hard_dir_cycles'])} / "
                f"文件 {len(result['hard_file_cycles'])}),按 --fail-on-hard-cycle 阻断。",
                flush=True,
            )
        return 1
    if comparison is not None and comparison.has_new:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
