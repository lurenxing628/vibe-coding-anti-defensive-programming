"""symbol-locator 命令入口:whereis / callers / callees。

用法:
  python -m tools.symbol_locator whereis <函数名> [--at FILE:LINE] [--json] [--rebuild]
  python -m tools.symbol_locator callers <函数名> [--deep] [--json] [--rebuild]
  python -m tools.symbol_locator callees <函数名> [--deep] [--json] [--rebuild]

产物目录由环境变量 CHECKUP_CALLGRAPH 覆盖(与 callgraph_extract 一致),
默认 .checkup/callgraph/。
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Tuple

from . import render, static_index


def _parse_at(value):
    # type: (str) -> Tuple[str, int]
    """``FILE:LINE`` -> (file, line);用 rpartition 兼容 Windows 盘符冒号。"""
    head, sep, tail = value.rpartition(":")
    if not sep or not head or not tail.isdigit() or int(tail) <= 0:
        raise argparse.ArgumentTypeError("--at 需要 FILE:LINE,且 LINE 必须是大于 0 的源码行号")
    return (head, int(tail))


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        prog="symbol-locator",
        description="函数定位/影响面查询(静态图秒查 + jedi 实时消歧)")
    sub = parser.add_subparsers(dest="cmd")
    build = sub.add_parser("build-index", help="重建 SCIP 深度索引")
    build.add_argument("--json", action="store_true", help="输出结构化 JSON")
    build.add_argument("--output", default=None, help="索引输出路径(默认 .checkup/scip/index.scip)")
    specs = (
        ("whereis", "查函数定义位置"),
        ("callers", "查谁调用它(改动影响面)"),
        ("callees", "查它调用谁(依赖)"),
    )
    for cmd_name, help_ in specs:
        p = sub.add_parser(cmd_name, help=help_)
        p.add_argument("symbol", help="函数名(裸名)")
        p.add_argument("--json", action="store_true", help="输出结构化 JSON")
        p.add_argument("--rebuild", action="store_true", help="强制重建静态图快照(~2s)")
        if cmd_name in ("callers", "callees"):
            p.add_argument("--deep", action="store_true", help="用 SCIP 索引做深度查询(含 tests)")
        if cmd_name == "whereis":
            p.add_argument(
                "--at", metavar="FILE:LINE", type=_parse_at,
                help="某调用点位置,用 jedi 精确判定该处指向哪个定义(消歧同名碰撞)")
    return parser


def _ensure_fresh(force, as_json):
    # type: (bool, bool) -> None
    from . import freshness
    if force or freshness.is_stale():
        reason = "强制 --rebuild" if force else "源码已变,快照过期"
        sys.stderr.write(f"[symbol-locator] {reason},重建静态图(~2s)...\n")
        if not freshness.rebuild():
            sys.stderr.write("[symbol-locator] 重建失败,沿用旧快照\n")
            hint = freshness.rebuild_error_hint()
            if hint:
                sys.stderr.write(f"[symbol-locator] {hint}\n")
    if not as_json:
        sys.stderr.write(f"[快照 {freshness.snapshot_label()}]\n")


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not args.cmd:
        parser.print_help()
        return 2
    if args.cmd == "build-index":
        return _build_scip_index(args.output, args.json)
    _ensure_fresh(args.rebuild, args.json)
    index = static_index.load()
    if args.cmd == "whereis":
        return render.render_whereis(args.symbol, index, as_json=args.json, at=args.at)
    return render.render_relation(args.symbol, index, args.cmd, as_json=args.json, deep=args.deep)


def _build_scip_index(output, as_json):
    # type: (Optional[str], bool) -> int
    import json

    from . import scip_deep
    try:
        path = scip_deep.build_index(index_path=output, stdout=sys.stderr if as_json else None)
    except scip_deep.ScipUnavailable as exc:
        if as_json:
            print(json.dumps({"engine": "scip", "error": exc.code, "message": exc.message, "hints": exc.hints},
                             ensure_ascii=False, indent=2))
        else:
            print(f"[scip 建索引不可用] {exc.message}")
            for hint in exc.hints:
                print(f"  - {hint}")
        return 3
    if as_json:
        print(json.dumps({"engine": "scip", "index": path}, ensure_ascii=False, indent=2))
    else:
        print(path)
    return 0
