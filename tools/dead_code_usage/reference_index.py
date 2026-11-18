"""公开引用索引：``dead_code_usage`` 对外的唯一入口。

``build_reference_index`` 解析给定文件，跑类型传播，给出：

- ``references``：每个函数（调用图限定名）与每个模块级变量（``rel::NAME``）的全部已解析引用。
  函数引用的 kind 见 ``model``：call / constructor 附实参绑定（``binder.Binding``），reference 是非调用
  引用即逃逸，property / protocol / decorator / callback 是隐式使用；变量引用的 kind 是 load / store /
  delete，附祖先链 context；import、def、class 重新绑定模块级变量也记 store（节点是该语句，context 为空）。
  库调用赋给外部属性的值（``protocols.EXTERNAL_ATTRIBUTE_SLOTS``）时，这次调用按库的真实形状合成在赋值处，
  记为 call，绑定里的实参是库传入的值。每条引用带记下它的节点的
  完整跨度（``Reference.span``），起点相同的嵌套节点各算各的。
  每条引用与未解析访问都带来源 origin（``Reference.origin``）：生产与工具代码自己就给出的取所在文件的角色，
  加入测试代码后才出现的记测试角色；角色由调用方经 ``CodeRoles`` 给出。
- ``unresolved_attributes``：接收者类型没能解析的属性访问，按属性名分组；同名函数若有这类访问，
  就可能存在没解析到的调用。
- ``functions``：每个函数的定义节点与签名（含默认值表达式）。
- ``module_scopes``：每个模块顶层的词法作用域（类型传播用的同一份），供消费方判断名字绑定在哪。

消费方据此下结论时，未解析、逃逸、不可判定的绑定一律不能当成“没有其他调用”。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .ast_nodes import node_span
from .binder import Signature
from .model import CALLBACK, AttributeSite, CodeRoles, FunctionRecord, Reference
from .program import FunctionInfo, Program
from .protocols import EXTERNAL_CALLBACKS
from .scope import Scope
from .solver import Solver
from .walker import Walker

_CALLBACK_NAMES = frozenset(name for names in EXTERNAL_CALLBACKS.values() for name in names)


@dataclass(frozen=True)
class FunctionDefinition:
    qual: str
    rel: str
    line: int
    signature: Signature


@dataclass(frozen=True)
class ReferenceIndex:
    references: Dict[str, Tuple[Reference, ...]]
    unresolved_attributes: Dict[str, Tuple[AttributeSite, ...]]
    functions: Dict[str, FunctionDefinition]
    module_scopes: Dict[str, Scope]


def build_reference_index(repo_root: str, records: Dict[str, FunctionRecord], rels: Sequence[str],
                          roles: CodeRoles) -> ReferenceIndex:
    """rels 是要解析的文件（仓库相对路径，调用方按路径角色给出）；records 是调用图给出的函数身份；
    roles 是证据来源用的代码角色。

    分两段求解：先只遍历生产与工具代码到不动点，这时的引用与未解析访问是生产证据（来源取所在文件的角色）；
    再在同一份状态上加入测试代码继续求解，新出现的都记为测试证据。两段都有的按生产证据留一条；
    生产段里有、加入测试后因拓宽不再出现的也留下，生产代码自己就给出了它。
    """
    program = Program(repo_root, records, rels)
    solver = Solver(program)
    solver.solve(Walker, lambda unit: roles.of(unit.rel) != roles.test, roles.of)
    production_refs, production_unresolved = solver.evidence()
    solver.solve(Walker, lambda unit: roles.of(unit.rel) == roles.test, lambda rel: roles.test)
    complete_refs, complete_unresolved = solver.evidence()
    found: List[Reference] = [*production_refs, *_callback_references(program, roles), *complete_refs]
    # 同一位置、同一种类的引用只留一条（先到的生产证据优先）；同一调用点的不同绑定（如经实例与经类调用同一方法）各留一条。
    grouped: Dict[str, Dict[Tuple, Reference]] = defaultdict(dict)
    for reference in found:
        grouped[reference.target].setdefault(_order(reference), reference)
    unresolved: Dict[str, Dict[Tuple, AttributeSite]] = defaultdict(dict)
    for site in [*production_unresolved, *complete_unresolved]:
        unresolved[site.name].setdefault((site.source_rel, site.line, site.col, site.source_qual), site)
    return ReferenceIndex(
        references={target: tuple(rows[key] for key in sorted(rows)) for target, rows in grouped.items()},
        unresolved_attributes={name: tuple(sites[key] for key in sorted(sites)) for name, sites in unresolved.items()},
        functions={qual: _definition(function) for qual, function in program.by_qual.items()},
        module_scopes={rel: module.scope for rel, module in program.modules.items()},
    )


def _order(reference: Reference) -> Tuple:
    """引用的去重与排序键：节点跨度、种类、来源，加上绑定的形状（各形参对到哪个位置的实参）。"""
    binding = reference.binding
    shape: Tuple = () if binding is None else (
        binding.decidable,
        () if binding.receiver is None else (binding.receiver,),
        tuple((name, expr.lineno, expr.col_offset) for name, expr in binding.arguments),
        binding.defaulted,
        tuple((expr.lineno, expr.col_offset) for expr in binding.var_positional),
        tuple((name, expr.lineno, expr.col_offset) for name, expr in binding.var_keyword),
    )
    return reference.source_rel, reference.span, reference.kind, reference.detail, reference.source_qual, shape


def _definition(function: FunctionInfo) -> FunctionDefinition:
    assert function.signature is not None
    return FunctionDefinition(function.qual, function.rel, function.node.lineno, function.signature)


def _callback_references(program: Program, roles: CodeRoles) -> List[Reference]:
    """类的真实继承链上有登记的外部基类时，它覆写的确切回调方法由库调用。"""
    found: List[Reference] = []
    for cls in program.classes.values():
        candidates = [function for name, function in cls.methods.items() if name in _CALLBACK_NAMES]
        if not candidates:
            continue
        ancestry = program.external_ancestry(cls.cid)
        for function in candidates:
            for base, methods in EXTERNAL_CALLBACKS.items():
                if base in ancestry and function.name in methods:
                    line, col, end_line, end_col = node_span(function.node)
                    found.append(Reference(
                        function.qual, cls.rel, line, col, end_line, end_col, "<module>", CALLBACK, base, roles.of(cls.rel),
                    ))
    return found
