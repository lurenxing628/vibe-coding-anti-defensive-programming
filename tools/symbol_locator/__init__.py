"""symbol-locator:函数定位/影响面查询小工具。

三引擎分工:
- static_index:既有 callgraph_extract 产物(functions.json/edges.json)秒查全量调用边——主路。
- jedi_resolver:jedi 在调用点实时消歧同名/attr 盲区(--at)——lazy import。
- scip_deep:scip-python 离线全量语义索引做精确影响面枚举(--deep)——lazy import。

约束:工具跑系统 python3.14,只读 Py3.8 项目代码;本包源码保持 Py3.8 语法(过 scan_py38plus_syntax 门禁);
jedi/scip 一律 lazy import,否则 .venv(3.8,无 jedi)导入本包即崩。
"""
