"""静态调用图加载器:读 callgraph_extract 产物,建查询索引。

数据源(既有资产,不新造):
- functions.json:dict,key=``rel::qual``,value 含 rel/line/end/cls/name/fan_in/fan_out。
- edges.json:list,每条 ``{from, to, kind, ambiguous}``,from/to 同为 ``rel::qual`` 主键。
- dynamic_unresolved.json:list,每条 ``{func, rel, line, hints}``,func 为 ``rel::qual``——
  静态图画不出的动态调用点(getattr 等),用于给 callees 标"实际被调可能更多"盲区。

confident 边(ambiguous=False)与 ambiguous 边分开存:主结果只给 confident,
ambiguous 仅以计数形式作盲区提示,不灌进主结果。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

FUNCTIONS_FILE = "functions.json"
EDGES_FILE = "edges.json"
DYNAMIC_FILE = "dynamic_unresolved.json"


def callgraph_dir(repo_root: str) -> str:
    """静态图产物目录的唯一解析,返回绝对路径;读取与重建都用这一个结果。

    CHECKUP_CALLGRAPH 覆盖默认的 .checkup/callgraph,相对路径按当前工作目录解析
    (与直接运行 callgraph_extract 一致)。
    """
    return os.path.abspath(
        os.environ.get("CHECKUP_CALLGRAPH")
        or os.path.join(repo_root, ".checkup", "latest", "callgraph"))


class StaticIndex:
    """functions.json + edges.json + dynamic_unresolved.json 的内存查询视图。"""

    def __init__(self, functions, edges, dynamic):
        self.functions = functions            # key(rel::qual) -> info dict
        self.edges = edges                     # list of {from,to,kind,ambiguous}
        self._by_name = defaultdict(list)      # bare name -> [key]
        self._callers = defaultdict(list)      # to(key)   -> [from key]  (confident)
        self._callees = defaultdict(list)      # from(key) -> [to key]    (confident)
        self._amb_callers = defaultdict(list)  # to(key)   -> [from key]  (ambiguous)
        self._amb_callees = defaultdict(list)  # from(key) -> [to key]    (ambiguous)
        self._dynamic = defaultdict(int)       # func(key) -> 动态调用点数
        for entry in dynamic:
            self._dynamic[entry["func"]] += 1
        self._build()

    def _build(self):
        for key, info in self.functions.items():
            self._by_name[info["name"]].append(key)
        for edge in self.edges:
            if edge["ambiguous"]:
                self._amb_callers[edge["to"]].append(edge["from"])
                self._amb_callees[edge["from"]].append(edge["to"])
            else:
                self._callers[edge["to"]].append(edge["from"])
                self._callees[edge["from"]].append(edge["to"])

    def lookup_name(self, name: str) -> List[str]:
        """裸名 -> 所有定义 key(同名碰撞时返回多个)。"""
        return self._by_name.get(name, [])

    def all_names(self) -> Iterable[str]:
        return self._by_name.keys()

    def info(self, key: str) -> Optional[Dict]:
        return self.functions.get(key)

    def callers_of(self, key: str) -> List[str]:
        return self._callers.get(key, [])

    def callees_of(self, key: str) -> List[str]:
        return self._callees.get(key, [])

    def ambiguous_callers_of(self, key: str) -> List[str]:
        return self._amb_callers.get(key, [])

    def ambiguous_callees_of(self, key: str) -> List[str]:
        return self._amb_callees.get(key, [])

    def dynamic_count(self, key: str) -> int:
        """该函数内部的动态调用点数(>0 表示其 callees 可能不全)。"""
        return self._dynamic.get(key, 0)


def load(graph_dir: str) -> StaticIndex:
    with open(os.path.join(graph_dir, FUNCTIONS_FILE), encoding="utf-8") as fh:
        functions = json.load(fh)
    with open(os.path.join(graph_dir, EDGES_FILE), encoding="utf-8") as fh:
        edges = json.load(fh)
    with open(os.path.join(graph_dir, DYNAMIC_FILE), encoding="utf-8") as fh:
        dynamic = json.load(fh)
    return StaticIndex(functions, edges, dynamic)
