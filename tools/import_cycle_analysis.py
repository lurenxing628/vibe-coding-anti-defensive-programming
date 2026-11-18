from __future__ import annotations

import ast
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from tools import import_cycle_graph as _graph


def classify(tree: ast.AST) -> List[Tuple[ast.AST, str]]:
    out: List[Tuple[ast.AST, str]] = []
    for statement in tree.body:  # type: ignore[attr-defined]
        _classify_statement(statement, "hard", out)
    return out


def _classify_statement(statement: ast.AST, context: str, out: List[Tuple[ast.AST, str]]) -> None:
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        out.append((statement, context))
        return
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _classify_body(statement.body, "typeonly" if context == "typeonly" else "lazy", out)
        return
    if isinstance(statement, ast.ClassDef):
        _classify_body(statement.body, context, out)
        return
    if isinstance(statement, ast.If):
        _classify_if(statement, context, out)
        return
    if isinstance(statement, ast.Try):
        _classify_try(statement, context, out)
        return
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        _classify_body(statement.body, context, out)
        _classify_body(statement.orelse, context, out)
        return
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        _classify_body(statement.body, context, out)


def _classify_body(statements: Sequence[ast.AST], context: str, out: List[Tuple[ast.AST, str]]) -> None:
    for statement in statements:
        _classify_statement(statement, context, out)


def _is_type_checking_test(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _conditional_context(context: str) -> str:
    return context if context in ("lazy", "typeonly") else "cond"


def _classify_if(statement: ast.If, context: str, out: List[Tuple[ast.AST, str]]) -> None:
    if _is_type_checking_test(statement.test):
        _classify_body(statement.body, "typeonly", out)
        _classify_body(statement.orelse, context, out)
        return
    branch_context = _conditional_context(context)
    _classify_body(statement.body, branch_context, out)
    _classify_body(statement.orelse, branch_context, out)


def _classify_try(statement: ast.Try, context: str, out: List[Tuple[ast.AST, str]]) -> None:
    branch_context = _conditional_context(context)
    _classify_body(statement.body, branch_context, out)
    for handler in statement.handlers:
        _classify_body(handler.body, branch_context, out)
    _classify_body(statement.orelse, branch_context, out)
    _classify_body(statement.finalbody, context, out)


ResolvedDynamicImport = Union[_graph.ResolvedFileImport, _graph.ResolvedNameImport]
UnresolvedDynamicImport = _graph.UnresolvedDynamicImport
LoaderCandidates = Dict[str, Set[str]]


def _dynamic_loader_kind(func: ast.AST, aliases: Dict[str, str]) -> Optional[str]:
    if isinstance(func, ast.Name):
        alias_kind = aliases.get(func.id)
        if alias_kind in {"__import__", "import_module", "spec_from_file_location"}:
            return alias_kind
        return None
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "import_module" and isinstance(func.value, ast.Name):
        return "import_module" if aliases.get(func.value.id) == "importlib" else None
    if func.attr != "spec_from_file_location":
        return None
    if isinstance(func.value, ast.Name):
        return "spec_from_file_location" if aliases.get(func.value.id) == "importlib.util" else None
    if (
        isinstance(func.value, ast.Attribute)
        and func.value.attr == "util"
        and isinstance(func.value.value, ast.Name)
        and aliases.get(func.value.value.id) == "importlib"
    ):
        return "spec_from_file_location"
    return None


def _is_unproven_dynamic_loader(func: ast.AST, candidates: LoaderCandidates) -> bool:
    if isinstance(func, ast.Name):
        return bool(candidates.get(func.id, set()) & {"__import__", "import_module", "spec_from_file_location"})
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "import_module" and isinstance(func.value, ast.Name):
        return "importlib" in candidates.get(func.value.id, set())
    if func.attr != "spec_from_file_location":
        return False
    if isinstance(func.value, ast.Name):
        return "importlib.util" in candidates.get(func.value.id, set())
    return bool(
        isinstance(func.value, ast.Attribute)
        and func.value.attr == "util"
        and isinstance(func.value.value, ast.Name)
        and "importlib" in candidates.get(func.value.value.id, set())
    )


def _dynamic_argument_text(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return ascii(node.value)
    if isinstance(node, ast.Attribute):
        return f"{_dynamic_argument_text(node.value)}.{node.attr}"
    if isinstance(node, ast.BinOp):
        operator = node.op.__class__.__name__
        return f"{_dynamic_argument_text(node.left)} {operator} {_dynamic_argument_text(node.right)}"
    if isinstance(node, ast.Call):
        return f"{_dynamic_argument_text(node.func)}(...)"
    return f"<{node.__class__.__name__}>"


class _DynamicImportVisitor(ast.NodeVisitor):
    def __init__(self, source_path: Optional[str], file_to_mod: Optional[Dict[str, str]]) -> None:
        self.context = "hard"
        self.aliases = {"__import__": "__import__"}
        self.candidates: LoaderCandidates = {"__import__": {"__import__"}}
        self.scope_safe: Dict[str, str] = {}
        self.scope_kind = "root"
        self.class_outer_aliases: Dict[str, str] = {}
        self.class_outer_candidates: LoaderCandidates = {}
        self.values: Dict[str, _graph.StaticValue] = {"__package__": "__package__", "__name__": "__name__"}
        if source_path is not None:
            self.values["__file__"] = source_path
        self.file_to_mod = file_to_mod or {}
        self.scope_assignments: Dict[str, ast.AST] = {}
        self.class_outer_values: Dict[str, _graph.StaticValue] = {}
        self.scope_parts: Tuple[str, ...] = ()
        self.resolved: List[ResolvedDynamicImport] = []
        self.unresolved: List[UnresolvedDynamicImport] = []

    @property
    def current_scope(self) -> str:
        return ".".join(self.scope_parts) if self.scope_parts else "<module>"

    def _visit_in_context(self, nodes: Sequence[ast.AST], context: str, *, isolate: bool = False) -> None:
        previous_context = self.context
        previous_aliases = dict(self.aliases) if isolate else None
        previous_values = dict(self.values) if isolate else None
        self.context = context
        try:
            for node in nodes:
                self.visit(node)
        finally:
            self.context = previous_context
            if previous_aliases is not None:
                self.aliases = previous_aliases
            if previous_values is not None:
                self.values = previous_values

    def _visit_scope(
        self,
        scope: ast.AST,
        nodes: Sequence[ast.AST],
        context: str,
        kind: str,
        inherited_aliases: Dict[str, str],
        inherited_candidates: LoaderCandidates,
        inherited_values: Dict[str, _graph.StaticValue],
    ) -> None:
        previous = (
            self.aliases,
            self.candidates,
            self.scope_safe,
            self.scope_kind,
            self.class_outer_aliases,
            self.class_outer_candidates,
            self.values,
            self.scope_assignments,
            self.class_outer_values,
            self.scope_parts,
        )
        bindings = _graph.import_scope(scope)
        aliases = {} if bindings.wildcard else dict(inherited_aliases)
        values = {} if bindings.wildcard else dict(inherited_values)
        for name in bindings.bound:
            aliases.pop(name, None)
            values.pop(name, None)
        candidates = {name: set(kinds) for name, kinds in inherited_candidates.items()}
        for name, kinds in bindings.candidates.items():
            candidates.setdefault(name, set()).update(kinds)
        self.aliases, self.candidates, self.scope_safe, self.scope_kind = aliases, candidates, bindings.aliases, kind
        self.values, self.scope_assignments = values, bindings.assignments
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.scope_parts = (*self.scope_parts, scope.name)
        elif isinstance(scope, ast.Lambda):
            self.scope_parts = (*self.scope_parts, "<lambda>")
        if kind == "class":
            self.class_outer_aliases = dict(inherited_aliases)
            self.class_outer_candidates = {
                name: set(kinds) for name, kinds in inherited_candidates.items()
            }
            self.class_outer_values = dict(inherited_values)
        try:
            self._visit_in_context(nodes, context)
        finally:
            (
                self.aliases,
                self.candidates,
                self.scope_safe,
                self.scope_kind,
                self.class_outer_aliases,
                self.class_outer_candidates,
                self.values,
                self.scope_assignments,
                self.class_outer_values,
                self.scope_parts,
            ) = previous

    def _nested_scope_sources(self) -> Tuple[Dict[str, str], LoaderCandidates, Dict[str, _graph.StaticValue]]:
        if self.scope_kind == "class":
            return self.class_outer_aliases, self.class_outer_candidates, self.class_outer_values
        return self.aliases, self.candidates, self.values

    def _activate_loader_imports(self, node: ast.AST) -> None:
        for name, kind in _graph.loader_import_bindings(node):
            if kind is not None and self.scope_safe.get(name) == kind:
                self.aliases[name] = kind

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_scope(node, node.body, "hard", "module", self.aliases, self.candidates, self.values)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        for target in node.targets:
            self._activate_value(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self._activate_value(node.target, node.value)

    def _activate_value(self, target: ast.AST, expression: ast.AST) -> None:
        if isinstance(target, ast.Name) and self.scope_assignments.get(target.id) is expression:
            value = _graph.static_import_value(expression, self.values, self.aliases)
            if value is not None:
                self.values[target.id] = value

    def _visit_function_header(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        args = node.args
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in args.defaults + [value for value in args.kw_defaults if value is not None]:
            self.visit(default)
        parameters = args.posonlyargs + args.args + args.kwonlyargs
        for arg in parameters + [arg for arg in (args.vararg, args.kwarg) if arg is not None]:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: Union[ast.FunctionDef, ast.AsyncFunctionDef]) -> None:
        self._visit_function_header(node)
        body_context = "typeonly" if self.context == "typeonly" else "lazy"
        self._visit_scope(node, node.body, body_context, "function", *self._nested_scope_sources())

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults + [value for value in node.args.kw_defaults if value is not None]:
            self.visit(default)
        body_context = "typeonly" if self.context == "typeonly" else "lazy"
        self._visit_scope(node, [node.body], body_context, "function", *self._nested_scope_sources())

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in node.decorator_list + node.bases:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._visit_scope(node, node.body, self.context, "class", *self._nested_scope_sources())

    def visit_Import(self, node: ast.Import) -> None:
        self._activate_loader_imports(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._activate_loader_imports(node)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        if _is_type_checking_test(node.test):
            self._visit_in_context(node.body, "typeonly", isolate=True)
            self._visit_in_context(node.orelse, self.context, isolate=True)
            return
        branch_context = _conditional_context(self.context)
        self._visit_in_context(node.body, branch_context, isolate=True)
        self._visit_in_context(node.orelse, branch_context, isolate=True)

    def visit_Try(self, node: ast.Try) -> None:
        branch_context = _conditional_context(self.context)
        self._visit_in_context(node.body, branch_context, isolate=True)
        for handler in node.handlers:
            if handler.type is not None:
                self._visit_in_context([handler.type], branch_context, isolate=True)
            self._visit_in_context(handler.body, branch_context, isolate=True)
        self._visit_in_context(node.orelse, branch_context, isolate=True)
        self._visit_in_context(node.finalbody, self.context)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.target)
        self.visit(node.iter)
        self._visit_in_context(node.body, self.context, isolate=True)
        self._visit_in_context(node.orelse, self.context, isolate=True)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # type: ignore[arg-type]

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._visit_in_context(node.body, self.context, isolate=True)
        self._visit_in_context(node.orelse, self.context, isolate=True)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self.visit(item.optional_vars)
        self._visit_in_context(node.body, self.context, isolate=True)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self.visit_With(node)  # type: ignore[arg-type]

    def visit_Call(self, node: ast.Call) -> None:
        loader_kind = _dynamic_loader_kind(node.func, self.aliases)
        first_arg = _call_argument(node, 0, "name")
        if loader_kind:
            if first_arg is None:
                self.unresolved.append(
                    _graph.UnresolvedDynamicImport(
                        self.context, node.lineno, self.current_scope, loader_kind,
                        f"{loader_kind}(<missing name>)", _graph.canonical_call_ast(node),
                    )
                )
            elif loader_kind == "spec_from_file_location":
                self._record_file_import(node, first_arg)
            else:
                self._record_name_import(node, first_arg, loader_kind)
        elif _is_unproven_dynamic_loader(node.func, self.candidates):
            argument = "<missing name>" if first_arg is None else _dynamic_argument_text(first_arg)
            expression = f"unproven loader binding: {_dynamic_argument_text(node.func)}({argument})"
            self.unresolved.append(
                _graph.UnresolvedDynamicImport(
                    self.context, node.lineno, self.current_scope, "unproven",
                    expression, _graph.canonical_call_ast(node),
                )
            )
        self.generic_visit(node)

    def _record_name_import(self, node: ast.Call, first_arg: ast.AST, loader_kind: str) -> None:
        call_ast = _graph.canonical_call_ast(node)
        target = _graph.static_import_value(first_arg, self.values, self.aliases)
        if isinstance(target, str):
            package_arg = _call_argument(node, 1, "package")
            package = _graph.static_import_value(package_arg, self.values, self.aliases) if package_arg else None
            package = package if target.startswith(".") and isinstance(package, str) else None
            if not target.startswith(".") or package is not None:
                self.resolved.append(
                    _graph.ResolvedNameImport(
                        target, package, self.context, node.lineno, self.current_scope, loader_kind, call_ast
                    )
                )
                return
            expression = f"relative target {target!a} with unresolved package"
        else:
            argument = _dynamic_argument_text(first_arg)
            expression = argument if loader_kind == "import_module" else f"{loader_kind}({argument})"
        self.unresolved.append(
            _graph.UnresolvedDynamicImport(
                self.context, node.lineno, self.current_scope, loader_kind, expression, call_ast
            )
        )

    def _record_file_import(self, node: ast.Call, first_arg: ast.AST) -> None:
        location = _call_argument(node, 1, "location")
        value = _graph.static_import_value(location, self.values, self.aliases) if location else None
        target = _graph.source_file_target(value, self.file_to_mod)
        name = _graph.static_import_value(first_arg, self.values, self.aliases)
        standard_args = len(node.args) <= 2 and all(k.arg in {"name", "location"} for k in node.keywords)
        if target is not None and isinstance(name, str) and standard_args:
            self.resolved.append(_graph.ResolvedFileImport(target, None, self.context, node.lineno))
        else:
            expression = f"spec_from_file_location({_dynamic_argument_text(first_arg)})"
            self.unresolved.append(
                _graph.UnresolvedDynamicImport(
                    self.context, node.lineno, self.current_scope, "spec_from_file_location",
                    expression, _graph.canonical_call_ast(node),
                )
            )


def _call_argument(call: ast.Call, position: int, *keyword_names: str) -> Optional[ast.AST]:
    if len(call.args) > position:
        return call.args[position]
    allowed = set(keyword_names)
    for keyword in call.keywords:
        if keyword.arg in allowed:
            return keyword.value
    return None


def dyn_imports(
    tree: ast.AST, *, source_path: Optional[str] = None, file_to_mod: Optional[Dict[str, str]] = None,
) -> Tuple[List[ResolvedDynamicImport], List[UnresolvedDynamicImport]]:
    visitor = _DynamicImportVisitor(source_path, file_to_mod)
    visitor.visit(tree)
    return visitor.resolved, visitor.unresolved


def resolve_targets(
    node: ast.AST,
    pkg: List[str],
    mod_to_file: Dict[str, str],
    *,
    include_parent_packages: bool = True,
) -> List[str]:
    if isinstance(node, ast.ImportFrom):
        targets = _import_from_targets(node, pkg, mod_to_file)
    else:
        targets = _direct_import_targets(node, mod_to_file)
    return expand_parent_packages(targets, mod_to_file) if include_parent_packages else targets


def expand_parent_packages(targets: Sequence[str], mod_to_file: Dict[str, str]) -> List[str]:
    expanded: List[str] = []
    for target in targets:
        parts = target.split(".")
        for length in range(1, len(parts)):
            parent = ".".join(parts[:length])
            if parent in mod_to_file and parent not in expanded:
                expanded.append(parent)
        if target not in expanded:
            expanded.append(target)
    return expanded


def _resolve_rel(pkg: List[str], level: int, module: Optional[str]) -> str:
    base = pkg[: len(pkg) - (level - 1)]
    return ".".join(base + module.split(".")) if module else ".".join(base)


def _import_from_base(node: ast.ImportFrom, pkg: List[str]) -> Optional[str]:
    return _resolve_rel(pkg, node.level, node.module) if (node.level and node.level > 0) else node.module


def _import_from_targets(node: ast.ImportFrom, pkg: List[str], mod_to_file: Dict[str, str]) -> List[str]:
    base = _import_from_base(node, pkg)
    if not base:
        return []
    names = [alias.name for alias in node.names]
    targets = [base + "." + name for name in names if (base + "." + name) in mod_to_file]
    if base in mod_to_file and not _all_names_are_child_modules(base, names, mod_to_file):
        targets.append(base)
    return targets


def _all_names_are_child_modules(base: str, names: List[str], mod_to_file: Dict[str, str]) -> bool:
    return bool(names) and all((base + "." + name) in mod_to_file for name in names)


def _direct_import_targets(node: ast.AST, mod_to_file: Dict[str, str]) -> List[str]:
    if not isinstance(node, ast.Import):
        return []
    return [alias.name for alias in node.names if alias.name in mod_to_file]
