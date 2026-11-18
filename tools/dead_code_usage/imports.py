"""模块索引与模块级名字的静态符号解析。

``module_symbols`` 沿 ``from x import y`` / ``import x`` 链追到名字最终指向的定义（函数、类、模块、
模块级变量或仓库外符号）。注解、基类、装饰器的身份与模块级变量的引用都走这里；类型传播里 import 语句
的取值也由它给出符号，再取符号的值。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .model import module_name_from_rel
from .protocols import ExternalSymbols
from .scope import Scope, build_scopes

DEF = "def"
CLASS = "class"
MODULE = "module"
EXTERNAL = "external"
VAR = "var"
_FROM = "from"


@dataclass(frozen=True)
class Symbol:
    kind: str
    module: str
    name: str
    node: Optional[ast.AST] = None


@dataclass(frozen=True)
class _Binding:
    kind: str
    node: ast.AST
    target: str = ""
    attr: str = ""


class ModuleInfo:
    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel = rel
        self.name = module_name_from_rel(rel)
        self.tree = tree
        self.is_package = rel.endswith("/__init__.py") or rel == "__init__.py"
        self.scopes: Dict[int, Scope] = build_scopes(tree)
        self.scope = self.scopes[id(tree)]
        self.top_bindings: Dict[str, List[_Binding]] = {}
        _collect_top_bindings(self, tree.body)

    def import_base(self, node: ast.ImportFrom) -> str:
        """``from ... import`` 的绝对基模块名。"""
        if not node.level:
            return node.module or ""
        parts = self.name.split(".") if self.is_package else self.name.split(".")[:-1]
        base = parts[: len(parts) - node.level + 1]
        if node.module:
            base.extend(node.module.split("."))
        return ".".join(base)


class ModuleIndex:
    def __init__(self, modules: Iterable[ModuleInfo], repo_root: str) -> None:
        """repo_root 是被扫描仓库的根目录；仓库外的符号由 externals 按这个仓库判定模块归属后读取。"""
        self.by_name: Dict[str, ModuleInfo] = {}
        self.by_rel: Dict[str, ModuleInfo] = {}
        self.packages: Set[str] = set()
        for info in modules:
            self.by_name[info.name] = info
            self.by_rel[info.rel] = info
            parts = info.name.split(".")
            self.packages.update(".".join(parts[:index]) for index in range(1, len(parts)))
        self.externals = ExternalSymbols(repo_root, self.is_module)
        self._cache: Dict[Tuple[str, str], Tuple[Symbol, ...]] = {}
        self._resolving: Set[Tuple[str, str]] = set()

    def is_module(self, name: str) -> bool:
        return name in self.by_name or name in self.packages

    def module_symbols(self, module: str, name: str) -> Tuple[Symbol, ...]:
        """模块 module 的全局名 name 最终指向的符号；同名多处绑定（条件定义、try 回退）全部给出。"""
        key = (module, name)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if key in self._resolving:
            return ()
        self._resolving.add(key)
        try:
            result = self._resolve(module, name)
        finally:
            self._resolving.discard(key)
        self._cache[key] = result
        return result

    def _resolve(self, module: str, name: str) -> Tuple[Symbol, ...]:
        info = self.by_name.get(module)
        bindings = info.top_bindings.get(name, []) if info is not None else []
        if not bindings:
            submodule = f"{module}.{name}"
            return (Symbol(MODULE, submodule, ""),) if self.is_module(submodule) else ()
        found: List[Symbol] = []
        for binding in bindings:
            found.extend(self._binding_symbols(module, name, binding))
        return tuple(dict.fromkeys(found))

    def _binding_symbols(self, module: str, name: str, binding: _Binding) -> Tuple[Symbol, ...]:
        if binding.kind in (DEF, CLASS, VAR):
            return (Symbol(binding.kind, module, name, binding.node),)
        if binding.kind == MODULE:
            return self.imported_module(binding.target)
        return self.imported_symbols(binding.target, binding.attr)

    def imported_module(self, dotted: str) -> Tuple[Symbol, ...]:
        """``import dotted`` 绑定到的符号：仓库模块，或别的导入目标（``_unindexed``）。"""
        return (Symbol(MODULE, dotted, ""),) if self.is_module(dotted) else self._unindexed(dotted)

    def imported_symbols(self, base: str, attr: str) -> Tuple[Symbol, ...]:
        """``from base import attr`` 绑定到的符号。"""
        return self.module_symbols(base, attr) if self.is_module(base) else self._unindexed(f"{base}.{attr}")

    def _unindexed(self, dotted: str) -> Tuple[Symbol, ...]:
        """不是已索引仓库模块的导入目标读到的外部符号（``ExternalSymbols.symbol``）。属于仓库的（首段已索引，如被忽略
        的本机文件导入已删除的模块；或来源在仓库目录里）不知道指向什么，没有符号，也不会被导入执行。"""
        symbol = self.externals.symbol(dotted)
        return () if symbol is None else (Symbol(EXTERNAL, symbol, ""),)


def _collect_top_bindings(info: ModuleInfo, body: Iterable[ast.stmt]) -> None:
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add(info, node.name, _Binding(DEF, node))
        elif isinstance(node, ast.ClassDef):
            _add(info, node.name, _Binding(CLASS, node))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    _add(info, alias.asname, _Binding(MODULE, node, alias.name))
                else:
                    head = alias.name.split(".")[0]
                    _add(info, head, _Binding(MODULE, node, head))
        elif isinstance(node, ast.ImportFrom):
            base = info.import_base(node)
            for alias in node.names:
                if alias.name != "*":
                    _add(info, alias.asname or alias.name, _Binding(_FROM, node, base, alias.name))
        else:
            for name in _stored_names(node):
                _add(info, name, _Binding(VAR, node))
            for child in _nested_bodies(node):
                _collect_top_bindings(info, child)


def _add(info: ModuleInfo, name: str, binding: _Binding) -> None:
    info.top_bindings.setdefault(name, []).append(binding)


def _stored_names(node: ast.stmt) -> List[str]:
    """语句自身（不进入子语句块）写入的名字。"""
    names: List[str] = []
    stack: List[ast.AST] = [child for child in ast.iter_child_nodes(node) if not isinstance(child, ast.stmt)]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Name) and not isinstance(current.ctx, ast.Load):
            names.append(current.id)
        elif isinstance(current, ast.NamedExpr):
            names.append(current.target.id)
        if isinstance(current, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        stack.extend(child for child in ast.iter_child_nodes(current) if not isinstance(child, ast.stmt))
    if isinstance(node, ast.Try):
        names.extend(handler.name for handler in node.handlers if handler.name)
    return names


def _nested_bodies(node: ast.stmt) -> List[List[ast.stmt]]:
    """模块顶层复合语句里仍属模块作用域的语句块。"""
    if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        return [node.body, node.orelse]
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return [node.body]
    if isinstance(node, ast.Try):
        return [node.body, node.orelse, node.finalbody, *(handler.body for handler in node.handlers)]
    return []
