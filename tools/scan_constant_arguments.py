"""恒定实参与恒空模块常量的只读审计扫描：不进门禁、不建基线，结果作人工审计的线索。

事实全部来自 ``tools/dead_code_usage`` 的公开引用索引（``build_reference_index``）及其实参绑定，两类发现：

- ``constant_argument``：有默认值的形参，生产与宿主工具代码对该函数的全部调用都没传，或都传同一个字面量
  （省略与显式传入同一个字面量默认值可以混用）。字面量指标量常量（含带符号数）和只由它们组成的元组。
- ``constant_empty``：模块里只赋值一次的空容器（``[]``、``{}``、``()``，或不带实参调用内置的 ``list``、``dict``、
  ``set``、``tuple``、``frozenset``：名字在模块作用域里没有别的绑定），生产与宿主工具代码对它的全部引用都是不改写、
  不传出的读取。

结论只看生产与宿主工具代码自己给出的证据（``Reference.origin``，角色见 ``tools/path_roles.py``）。测试证据
（测试文件里的引用，及测试传入的类型经生产代码形成的引用）不影响结论，与结论不符的逐条列出
（“只有测试传了别的值”“测试改写或传出了它”）。

索引是欠近似，不可判定一律不报。下结论前同时要求：

1. 生产/工具代码里没有同名的未解析属性访问；
2. 函数：生产/工具引用全是已解析调用，没有逃逸，也没有 property / protocol / decorator / callback 这类隐式使用，
   每次调用的绑定都可判定（调用处没有 ``*`` / ``**``、签名精确、形参对得上），同一调用点也只有一种绑定
   （经实例与经类取到同一方法时实参对到的形参不同）；
   变量：除定义外没有写入与删除（import、def、class 重新绑定同名也算写入），每处读取都在只读位置（迭代、
   真值、比较、运算、下标读取、只读方法等），作实参、返回值、别名或放进容器都算传出；作比较、运算、增量赋值的
   操作数或下标（含切片分量）时，接收它的另一方（另一操作数、``in`` 右边的容器、被取下标的对象、增量赋值的
   目标）不是字面量就可能经协议方法（``__eq__``、``__radd__``、``__contains__``、``__getitem__`` 等）把它当实参
   拿走，与规则 4 同一口径按传出算；
3. 至少一处来自别的函数的调用、至少一处读取：没有外部使用的不下“恒定”结论（那归死代码扫描）；
4. 以下情形的引用表不完整且不留标记，不判定：
   - 方法名在多个生产/工具类里都有定义：索引按流入的具体类型记账，经基类或协议注解的接收者调用时，
     覆写与协议实现会缺席；
   - dunder 名（函数、方法与模块级变量）：由解释器与导入机制按协议隐式使用（构造、调用、运算、``__all__``），
     类对象与实例作为值流出时索引不记逃逸，接收者没解析到的隐式调用也不留标记；
   - lambda：它的逃逸不入索引。

仍然看不见的引用：非常量名的 ``getattr``、``globals()`` / ``vars()``、星号导入、字符串里的代码（如 ``python -c``）。
结论是审计线索，动手前按调用方复核。

用法：
    python -m tools.scan_constant_arguments [--json] [--repo-root PATH]
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter, defaultdict
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from tools.dead_code_usage.binder import Binding
from tools.dead_code_usage.model import CALL, DELETE, LOAD, STORE, CodeRoles, Reference, rel_join
from tools.dead_code_usage.reference_index import FunctionDefinition, ReferenceIndex, build_reference_index
from tools.path_roles import ROLES, TEST, iter_role_files, role_of, role_roots
from tools.repo_paths import resolve_repo_root

TOOL = "constant_arguments"
CONSTANT_ARGUMENT = "constant_argument"
CONSTANT_EMPTY = "constant_empty"
KINDS = (CONSTANT_ARGUMENT, CONSTANT_EMPTY)
_EMPTY_CONSTRUCTORS = frozenset({"builtins.list", "builtins.dict", "builtins.set", "builtins.tuple", "builtins.frozenset"})
# 内置容器上不改写接收者、也不把接收者交出去的方法。
_READ_ONLY_METHODS = frozenset({
    "copy", "count", "index", "get", "items", "keys", "values",
    "difference", "intersection", "isdisjoint", "issubset", "issuperset", "symmetric_difference", "union",
})
_ESCAPES = (ast.Call, ast.Return, ast.Yield, ast.Await, ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.Raise,
            ast.Lambda, ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Assert)
_WRITE_LABELS = {STORE: "写入", DELETE: "删除"}

Value = Tuple[str, ...]  # ("literal", repr) / ("default",) 非字面量默认值 / ("other",) 非字面量实参
Use = Tuple[str, bool]  # (用途, 是否只读)
_HANDED_OVER: Use = ("传出", False)


class _Sources:
    """按文件缓存源码，取表达式原文作显示文本。"""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root
        self.texts: Dict[str, str] = {}

    def text(self, rel: str, node: ast.expr) -> str:
        if rel not in self.texts:
            with open(rel_join(self.repo_root, rel), encoding="utf-8") as handle:
                self.texts[rel] = handle.read()
        segment = ast.get_source_segment(self.texts[rel], node)
        assert segment is not None
        return segment


def _site(reference: Reference) -> str:
    return f"{reference.source_rel}:{reference.line}"


def _split_roles(references: Iterable[Reference]) -> Tuple[List[Reference], List[Reference]]:
    """按证据来源（``Reference.origin``）分成 (生产与宿主工具, 测试)。"""
    kept: List[Reference] = []
    tests: List[Reference] = []
    for reference in references:
        (tests if reference.origin == TEST else kept).append(reference)
    return kept, tests


def _has_unresolved(index: ReferenceIndex, name: str) -> bool:
    return any(site.origin != TEST for site in index.unresolved_attributes.get(name, ()))


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _binding(reference: Reference) -> Binding:
    """call 引用必附实参绑定。"""
    assert reference.binding is not None
    return reference.binding


# --- 恒定实参 -------------------------------------------------------------------------------------------


def _is_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float, complex)
    if isinstance(node, ast.Tuple):
        return all(_is_literal(element) for element in node.elts)
    return False


def _literal(node: ast.expr) -> Value:
    """字面量按求值结果的 repr 比较：``True`` 与 ``1``、``1`` 与 ``1.0`` 是不同的取值。"""
    return ("literal", repr(ast.literal_eval(node)))


def _value(reference: Reference, name: str, default: ast.expr) -> Tuple[Value, Optional[ast.expr]]:
    """一次可判定调用里形参的有效取值，及显式传入时的实参表达式（没传时为 None）。"""
    arguments = dict(_binding(reference).arguments)
    if name not in arguments:
        return (_literal(default) if _is_literal(default) else ("default",)), None
    passed = arguments[name]
    return (_literal(passed) if _is_literal(passed) else ("other",)), passed


def _name_of(qual: str) -> Tuple[str, bool]:
    """限定名 ``rel::[外层.]名字`` 的 (名字, 是否类的直接方法)；嵌套函数的上一级是 ``<locals>``。"""
    owner, _, name = qual.split("::", 1)[1].rpartition(".")
    return name, bool(owner) and not owner.endswith("<locals>")


def _shared_method_names(index: ReferenceIndex) -> FrozenSet[str]:
    """在两个以上生产/工具类里都有定义的方法名。"""
    names: Counter = Counter()
    for qual, definition in index.functions.items():
        name, is_method = _name_of(qual)
        if is_method and role_of(definition.rel) != TEST:
            names[name] += 1
    return frozenset(name for name, count in names.items() if count > 1)


def _decidable_calls(index: ReferenceIndex, qual: str, name: str) -> Optional[Tuple[List[Reference], List[Reference]]]:
    """生产/工具引用全是可判定的已解析调用、每个调用点只有一种绑定、且有来自别处的调用时，
    返回 (生产/工具调用, 测试引用)。索引对同一调用点的同种绑定只留一条，同一位置出现两条就是绑定有分歧。"""
    if _has_unresolved(index, name):
        return None
    calls, tests = _split_roles(index.references.get(qual, ()))
    if not calls or not all(reference.kind == CALL and _binding(reference).decidable for reference in calls):
        return None
    if len({(reference.source_rel, reference.line, reference.col) for reference in calls}) != len(calls):
        return None
    if all(reference.source_qual == qual for reference in calls):
        return None
    return calls, tests


def _parameter_finding(definition: FunctionDefinition, name: str, calls: List[Reference], tests: List[Reference],
                       sources: _Sources) -> Optional[Dict[str, Any]]:
    default = definition.signature.defaults[name]
    values = [_value(reference, name, default) for reference in calls]
    keys = {value for value, _passed in values}
    if len(keys) != 1 or ("other",) in keys:
        return None
    (constant,) = keys
    omitted = sum(1 for _value_key, passed in values if passed is None)
    source = "default" if omitted == len(values) else "literal" if omitted == 0 else "default_and_literal"
    default_text = sources.text(definition.rel, default)
    return {
        "kind": CONSTANT_ARGUMENT,
        "rel": definition.rel,
        "line": definition.line,
        "function": definition.qual,
        "parameter": name,
        "value": default_text if constant == ("default",) else constant[1],
        "value_source": source,
        "default": default_text,
        "calls": [_site(reference) for reference in calls],
        "test_deviations": _test_deviations(definition, name, constant, tests, sources),
    }


def _test_deviations(definition: FunctionDefinition, name: str, constant: Value, tests: List[Reference],
                     sources: _Sources) -> List[Dict[str, str]]:
    default = definition.signature.defaults[name]
    found: List[Dict[str, str]] = []
    for reference in tests:
        if reference.kind != CALL or not _binding(reference).decidable:
            found.append({"site": _site(reference), "value": f"不可判定的引用（{reference.kind}）"})
            continue
        value, passed = _value(reference, name, default)
        if value == constant:
            continue
        shown = "没传（用默认值）" if passed is None else f"传 {sources.text(reference.source_rel, passed)}"
        found.append({"site": _site(reference), "value": shown})
    return found


def _constant_arguments(index: ReferenceIndex, sources: _Sources) -> List[Dict[str, Any]]:
    shared = _shared_method_names(index)
    findings: List[Dict[str, Any]] = []
    for qual, definition in index.functions.items():
        name, is_method = _name_of(qual)
        if (not definition.signature.defaults or role_of(definition.rel) == TEST or name.startswith("<lambda>@")
                or _is_dunder(name) or (is_method and name in shared)):
            continue
        decided = _decidable_calls(index, qual, name)
        if decided is None:
            continue
        calls, tests = decided
        for parameter in definition.signature.defaults:
            finding = _parameter_finding(definition, parameter, calls, tests, sources)
            if finding is not None:
                findings.append(finding)
    return findings


# --- 恒空模块常量 ---------------------------------------------------------------------------------------


def _at(node: ast.AST, line: int, col: int) -> bool:
    return isinstance(node, ast.expr) and node.lineno == line and node.col_offset == col


def _child_at(parent: ast.AST, line: int, col: int) -> Optional[ast.expr]:
    """parent 里起始位置是 (line, col) 的表达式子节点。

    类型传播求值时不入祖先链的包装节点（comprehension、keyword、f-string 的 FormattedValue、3.8 的 Index/Slice、
    arguments、except 子句）向下穿过；不进入子语句，也不进入其他子表达式内部。
    """
    stack = list(ast.iter_child_nodes(parent))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.expr) and not isinstance(node, ast.FormattedValue):
            if _at(node, line, col):
                return node
        elif not isinstance(node, ast.stmt):
            stack.extend(ast.iter_child_nodes(node))
    return None


def _direct_use(parent: ast.AST, child: ast.expr, ancestors: Sequence[ast.AST]) -> Optional[Use]:
    """child 作为 parent 的直接子表达式时的用途；ancestors 是 parent 往外的各层（判断方法调用，以及 3.8 下标里的
    切片、扩展切片各维所属的下标）。

    返回 None 表示取值原样流进 parent 的结果（BoolOp、IfExp 的分支），用途由更外层决定。
    """
    if isinstance(parent, (ast.For, ast.AsyncFor, ast.YieldFrom, ast.Starred)):
        return ("迭代", True)
    if isinstance(parent, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        if any(child is generator.iter for generator in parent.generators):
            return ("迭代", True)
        if any(child is condition for generator in parent.generators for condition in generator.ifs):
            return ("真值", True)
        return _HANDED_OVER
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and child is parent.test:
        return ("真值", True)
    if isinstance(parent, (ast.BoolOp, ast.IfExp)):
        return None
    if isinstance(parent, ast.UnaryOp):
        return ("真值", True) if isinstance(parent.op, ast.Not) else ("运算", True)
    if isinstance(parent, ast.Compare):
        return _operand(_compared_with(parent, child), ("比较", True))
    if isinstance(parent, ast.BinOp):
        return _operand([parent.right if child is parent.left else parent.left], ("运算", True))
    if isinstance(parent, ast.JoinedStr):
        return ("格式化", True)
    if isinstance(parent, ast.Subscript):
        if child is not parent.value:
            return _operand([parent.value], ("作下标", True))
        return ("下标读取", True) if isinstance(parent.ctx, ast.Load) else ("下标改写", False)
    if isinstance(parent, (ast.Slice, ast.Index)):
        # 切片的分量、扩展切片（``a[x:, 0]``）的各维：整个下标交给最近的那层 Subscript 的被取对象。
        subscript = next(node for node in ancestors if isinstance(node, ast.Subscript))
        return _operand([subscript.value], ("作下标", True))
    if isinstance(parent, ast.Attribute):
        if not isinstance(parent.ctx, ast.Load):
            return ("属性改写", False)
        if isinstance(ancestors[0], ast.Call) and ancestors[0].func is parent:
            return (f"调用 .{parent.attr}()", parent.attr in _READ_ONLY_METHODS)
        return (f"取属性 .{parent.attr}", False)
    if isinstance(parent, ast.AugAssign):
        return _operand([parent.target], ("运算", True)) if child is parent.value else ("增量改写", False)
    if isinstance(parent, ast.Try) and any(child is handler.type for handler in parent.handlers):
        return ("异常匹配", True)
    if isinstance(parent, ast.Expr):
        return ("表达式语句", True)
    if isinstance(parent, _ESCAPES):
        return _HANDED_OVER
    return ("未识别", False)


def _operand(receivers: Sequence[ast.expr], use: Use) -> Use:
    """作比较、运算、增量赋值或下标的操作数：接收它的各方（另一操作数、被测容器、被取下标的对象、增量赋值的目标）
    都是字面量时只读；否则它可能作为实参交给对方的协议方法（``__eq__``、``__radd__``、``__getitem__`` 等可能改写或
    留存它）——对方是仓库对象、或类型未知可能是仓库对象——与规则 4 同一口径算传出。"""
    return use if all(_is_literal(receiver) for receiver in receivers) else _HANDED_OVER


def _compared_with(parent: ast.Compare, child: ast.expr) -> List[ast.expr]:
    """比较链里会以 child 为实参调用协议方法的一方：相邻的另一操作数；``in`` 左边的被测值交给右边的容器，
    右边的容器自己做包含判断（内置容器只比较它的元素）；``is`` 只比身份。"""
    operands = [parent.left, *parent.comparators]
    position = next(index for index, operand in enumerate(operands) if operand is child)
    receivers = []
    for index, operator in enumerate(parent.ops):
        if position not in (index, index + 1) or isinstance(operator, (ast.Is, ast.IsNot)):
            continue
        if isinstance(operator, (ast.In, ast.NotIn)):
            if position == index:
                receivers.append(operands[index + 1])
        else:
            receivers.append(operands[index + 1 if position == index else index])
    return receivers


def _variable_use(reference: Reference) -> Use:
    """模块级变量一次读取的用途：沿祖先链往上，直到取值不再原样流进外层表达式。"""
    context = reference.context
    child = _child_at(context[0], reference.line, reference.col) if context else None
    if child is None:
        return ("未识别", False)
    for depth, parent in enumerate(context):
        use = _direct_use(parent, child, context[depth + 1:])
        if use is not None:
            return use
        assert isinstance(parent, ast.expr)
        child = parent
    return ("未识别", False)


def _empty_container(index: ReferenceIndex, rel: str, value: Optional[ast.expr]) -> Optional[str]:
    """模块顶层的空容器写法：空字面量，或不带实参调用解析到内置的容器构造器（名字在模块作用域里没有别的绑定）。"""
    if isinstance(value, (ast.List, ast.Tuple)) and not value.elts:
        return "[]" if isinstance(value, ast.List) else "()"
    if isinstance(value, ast.Dict) and not value.keys:
        return "{}"
    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and not value.args and not value.keywords
            and index.module_scopes[rel].binding_scope(value.func.id) is None
            and f"builtins.{value.func.id}" in _EMPTY_CONSTRUCTORS):
        return f"{value.func.id}()"
    return None


def _definition_value(index: ReferenceIndex, rel: str, store: Reference) -> Optional[str]:
    """唯一一次写入是本模块顶层 ``NAME = 空容器`` / ``NAME: T = 空容器``（单目标，没有别名）时给出容器写法。
    import、def、class 绑定的写入没有祖先链，不是赋值。"""
    if store.source_rel != rel or store.source_qual != "<module>" or not store.context:
        return None
    statement = store.context[0]
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 \
            and _at(statement.targets[0], store.line, store.col):
        return _empty_container(index, rel, statement.value)
    if isinstance(statement, ast.AnnAssign) and _at(statement.target, store.line, store.col):
        return _empty_container(index, rel, statement.value)
    return None


def _empty_finding(index: ReferenceIndex, target: str, references: Sequence[Reference]) -> Optional[Dict[str, Any]]:
    rel, _, name = target.partition("::")
    if _is_dunder(name):
        return None
    kept, tests = _split_roles(references)
    stores = [reference for reference in kept if reference.kind != LOAD]
    if len(stores) != 1 or stores[0].kind != STORE or _has_unresolved(index, name):
        return None
    shown = _definition_value(index, rel, stores[0])
    reads = [(reference, _variable_use(reference)) for reference in kept if reference.kind == LOAD]
    if shown is None or not reads or not all(read for _reference, (_label, read) in reads):
        return None
    deviations = []
    for reference in tests:
        label, read = _variable_use(reference) if reference.kind == LOAD else (_WRITE_LABELS[reference.kind], False)
        if not read:
            deviations.append({"site": _site(reference), "value": label})
    return {
        "kind": CONSTANT_EMPTY,
        "rel": rel,
        "line": stores[0].line,
        "name": name,
        "value": shown,
        "reads": [{"site": _site(reference), "use": label} for reference, (label, _read) in reads],
        "test_deviations": deviations,
    }


def _constant_empties(index: ReferenceIndex) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for target, references in index.references.items():
        variable = [reference for reference in references if reference.kind in (LOAD, STORE, DELETE)]
        if variable:
            finding = _empty_finding(index, target, variable)
            if finding is not None:
                findings.append(finding)
    return findings


# --- 入口 -----------------------------------------------------------------------------------------------


def _order(item: Dict[str, Any]) -> Tuple[str, int, str, str]:
    subject = item["parameter"] if item["kind"] == CONSTANT_ARGUMENT else item["name"]
    return item["rel"], item["line"], item["kind"], subject


def scan(repo_root: str) -> Dict[str, Any]:
    rels = iter_role_files(repo_root, ROLES, (".py",))
    index = build_reference_index(repo_root, {}, rels, CodeRoles(role_of, TEST))
    sources = _Sources(repo_root)
    findings = _constant_arguments(index, sources) + _constant_empties(index)
    findings.sort(key=_order)
    totals = Counter(item["kind"] for item in findings)
    return {
        "tool": TOOL,
        "scan_roots": list(role_roots(ROLES)),
        "file_count": len(rels),
        "totals": {kind: totals[kind] for kind in KINDS},
        "findings": findings,
    }


def _render_finding(item: Dict[str, Any]) -> List[str]:
    if item["kind"] == CONSTANT_ARGUMENT:
        claim, how = {
            "default": (f"恒用默认值 {item['value']}", "都没传"),
            "literal": (f"恒传 {item['value']}（默认值 {item['default']} 从未生效）", "都传这个字面量"),
            "default_and_literal": (f"恒为 {item['value']}", "没传或显式传同值"),
        }[item["value_source"]]
        lines = [
            f"  :{item['line']} 恒定实参 {item['function'].split('::', 1)[1]}({item['parameter']}) {claim}"
            f"：{len(item['calls'])} 处生产/工具调用{how}（{', '.join(item['calls'])}）"
        ]
    else:
        reads = ", ".join(f"{read['site']} {read['use']}" for read in item["reads"])
        lines = [f"  :{item['line']} 恒空常量 {item['name']} = {item['value']}：{len(item['reads'])} 处生产/工具读取都只读（{reads}）"]
    if item["test_deviations"]:
        prefix = "只有测试传了别的值" if item["kind"] == CONSTANT_ARGUMENT else "只有测试改写或传出"
        lines.append(f"      {prefix}：" + "; ".join(f"{dev['site']} {dev['value']}" for dev in item["test_deviations"]))
    return lines


def render_text(result: Dict[str, Any]) -> str:
    totals = result["totals"]
    lines = [
        f"[{TOOL}] 扫描 {result['file_count']} 个文件：恒定实参 {totals[CONSTANT_ARGUMENT]} 处，"
        f"恒空模块常量 {totals[CONSTANT_EMPTY]} 处（只读审计，不进门禁；结论只看生产与宿主工具代码）"
    ]
    by_file: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in result["findings"]:
        by_file[item["rel"]].append(item)
    for rel in sorted(by_file):
        lines.append(rel)
        for item in by_file[rel]:
            lines.extend(_render_finding(item))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="恒定实参与恒空模块常量的只读审计扫描")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", default=resolve_repo_root(), help="要扫描的仓库根目录（默认本仓库）")
    args = parser.parse_args(argv)
    result = scan(args.repo_root)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
