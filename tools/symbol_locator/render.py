"""输出格式化:人看(file:line + 扇入扇出 + 盲区标注)与 --json(给 LLM)。

每个 render_* 既打印结果又返回进程退出码(0 命中 / 1 未找到/未解析)。
"""
from __future__ import annotations

import difflib
import json as _json
from typing import Dict, List, Optional

_MENU_MAX = 20  # 同名碰撞菜单最多列几条,超出折叠


def _loc(info):
    # type: (Dict) -> str
    return f"{info['rel']}:{info['line']}-{info.get('end', info['line'])}"


def _loc_full(key, info):
    # type: (str, Optional[Dict]) -> str
    if info is None:
        return key  # 指向未索引/三方函数,原样显示 key
    qual = key.split("::", 1)[-1]
    return f"{info['rel']}:{info['line']}  {qual}  (被调 {info.get('fan_in', 0)} · 调用 {info.get('fan_out', 0)})"


def render_whereis(name, index, as_json=False, at=None):
    # type: (str, object, bool, object) -> int
    keys = index.lookup_name(name)
    if not keys:
        return _not_found(name, index, as_json)
    if at is not None:
        return _render_whereis_at(name, at, index, as_json)
    if as_json:
        matches = _whereis_matches(keys, index)
        print(_json.dumps({"symbol": name, "matches": matches}, ensure_ascii=False, indent=2))
        return 0
    if len(keys) == 1:
        info = index.info(keys[0])
        print(f"{_loc(info)}   (被调 {info.get('fan_in', 0)} · 调用 {info.get('fan_out', 0)})")
        return 0
    # 同名碰撞:列 cls/rel 消歧菜单(折叠 + 提示用 --at 让 jedi 精确判定)
    print(f"{len(keys)} 处同名(按类/文件消歧):")
    for k in keys[:_MENU_MAX]:
        info = index.info(k)
        cls = info.get("cls")
        label = f"{cls}.{info['name']}" if cls else info["name"]
        print(f"  {label:<44} {_loc(info)}")
    if len(keys) > _MENU_MAX:
        print(f"  ... 还有 {len(keys) - _MENU_MAX} 处")
    print("→ 加 --at <file:line>(某个调用点)让 jedi 精确判定它指向哪个定义")
    return 0


def _render_whereis_at(name, at, index, as_json=False):
    # type: (str, object, object, bool) -> int
    from . import jedi_resolver
    file_rel, line = at
    hits = jedi_resolver.resolve_at(file_rel, line, name)
    if hits is None:
        reason = "missing_jedi" if not jedi_resolver.available() else "callpoint_not_resolved"
        if as_json:
            print(_json.dumps(
                {"symbol": name, "at": f"{file_rel}:{line}", "engine": "jedi",
                 "degraded": True, "reason": reason,
                 "matches": _whereis_matches(index.lookup_name(name), index)},
                ensure_ascii=False, indent=2))
            return 0
        if not jedi_resolver.available():
            print("(jedi 不可用,降级到静态结果;在工具解释器装 jedi 可启用精确消歧)")
        else:
            print(f"(在 {file_rel}:{line} 未定位到 {name!r} 的调用点,降级到静态结果)")
        return render_whereis(name, index, as_json=as_json)
    if as_json:
        print(_json.dumps(
            {"symbol": name, "at": f"{file_rel}:{line}",
             "resolved": [{"rel": r, "line": line_no, "full_name": fn} for r, line_no, fn in hits]},
            ensure_ascii=False, indent=2))
        return 0 if hits else 1
    if not hits:
        print(f"jedi 在 {file_rel}:{line} 未解析到 {name!r} 的定义")
        return 1
    print(f"{file_rel}:{line} 处 {name!r} 精确指向(jedi):")
    for rel, line_no, full_name in hits:
        loc = f"{rel}:{line_no}" if rel else "(stdlib/三方)"
        print(f"  {loc}  {full_name or ''}")
    return 0


def render_relation(name, index, direction, as_json=False, deep=False):
    # type: (str, object, str, bool, bool) -> int
    if deep:
        return _render_deep_relation(name, index, direction, as_json=as_json)
    keys = index.lookup_name(name)
    if not keys:
        return _not_found(name, index, as_json)
    payload = {}
    for k in keys:
        if direction == "callers":
            related = index.callers_of(k)
            ambiguous = len(index.ambiguous_callers_of(k))
            dynamic = 0
        else:
            related = index.callees_of(k)
            ambiguous = len(index.ambiguous_callees_of(k))
            dynamic = index.dynamic_count(k)
        payload[k] = {"related": related, "ambiguous": ambiguous, "dynamic": dynamic}
    if as_json:
        print(_json.dumps({"symbol": name, "direction": direction, "result": payload},
                          ensure_ascii=False, indent=2))
        return 0
    verb = "谁调用" if direction == "callers" else "调用了谁"
    for k, data in payload.items():
        qual = k.split("::", 1)[-1]
        related = data["related"]
        print(f"[{verb}] {qual}(全量 confident 边 {len(related)} 条):")
        for rk in related:
            print(f"  {_loc_full(rk, index.info(rk))}")
        if not related:
            print("  (无 confident 边)")
        _print_blindspots(data, direction)
    return 0


def _render_deep_relation(name, index, direction, as_json=False):
    # type: (str, object, str, bool) -> int
    from . import scip_deep
    try:
        payload = scip_deep.query(name, direction, index)
    except scip_deep.ScipUnavailable as exc:
        if as_json:
            print(_json.dumps(
                {
                    "symbol": name,
                    "direction": direction,
                    "engine": "scip",
                    "error": exc.code,
                    "message": exc.message,
                    "hints": exc.hints,
                },
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print(f"[scip 深度查询不可用] {exc.message}")
            for hint in exc.hints:
                print(f"  - {hint}")
        return 3
    if not payload["definitions"]:
        return _not_found(name, index, as_json)
    if as_json:
        print(_json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    verb = "谁调用" if direction == "callers" else "调用了谁"
    rows = payload["rows"]
    defs = payload["definitions"]
    tests_count = sum(1 for row in rows if row["path"].startswith("tests/"))
    print(f"[scip 全量精确] {verb} {name}:{len(rows)} 处(含 tests {tests_count} 处)")
    print(f"[scip 索引 {payload['index']['label']}] {payload['index']['path']}")
    if defs:
        print("目标定义:")
        for row in defs:
            print(f"  {row['path']}:{row['line']}  {row['display']}")
    if rows:
        print("结果:")
        for row in rows:
            print(f"  {row['path']}:{row['line']}  {row['display']}")
    else:
        print("结果:(无)")
    return 0


def _print_blindspots(data, direction):
    # type: (Dict, str) -> None
    notes = []
    if data["ambiguous"]:
        notes.append(f"另有 {data['ambiguous']} 条 ambiguous 边(消解不确定,未计入)")
    if direction == "callees" and data["dynamic"]:
        notes.append(f"该函数含 {data['dynamic']} 处动态调用(getattr 等),实际调用可能更多")
    notes.append("未含 tests/(静态图不扫测试目录)")
    print("  ⚠ 盲区:" + ";".join(notes))
    print("  → 加 --deep 用 scip 全量精确枚举(含 tests)")


def _not_found(name, index, as_json):
    # type: (str, object, bool) -> int
    near = difflib.get_close_matches(name, index.all_names(), n=5, cutoff=0.6)
    if as_json:
        print(_json.dumps({"symbol": name, "error": "not_found", "suggestions": near},
                          ensure_ascii=False))
    else:
        print(f"未找到函数 {name!r}")
        if near:
            print(f"近似名:{', '.join(near)}")
    return 1


def _whereis_matches(keys, index):
    # type: (List[str], object) -> List[Dict]
    matches = []
    for key in keys:
        info = index.info(key)
        matches.append({
            "key": key, "rel": info["rel"], "line": info["line"],
            "end": info.get("end"), "cls": info.get("cls"),
            "fan_in": info.get("fan_in", 0), "fan_out": info.get("fan_out", 0),
        })
    return matches
