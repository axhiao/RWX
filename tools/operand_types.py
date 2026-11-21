"""Collect operand type coverage for attribute identity comparisons on the base tree."""
import ast
import builtins
import enum
from functools import cache
import importlib.abc
import importlib.machinery
import sys


@cache
def type_category(cls):
    if cls is type(None):
        return "none"
    if cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
        return "identity"
    if issubclass(cls, enum.Enum):
        # These built-in implementations compare equal on a single enum class
        # exactly when its canonical members are identical.
        scalar = next((t for t in (str, int) if issubclass(cls, t)), None)
        if scalar and cls.__eq__ is scalar.__eq__ and cls.__ne__ is scalar.__ne__:
            return "enum:" + cls.__module__ + "." + cls.__qualname__
    if cls in (str, int, float, bool, bytes, tuple, list, dict, set, frozenset):
        return "builtin"
    return "other"


class Probe(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            return node
        if not isinstance(node.left, ast.Attribute):
            return node
        key = f"{self.path}:{node.lineno}:{node.col_offset}"
        probe = ast.Call(ast.Name("_blast_operands", ast.Load()),
                         [ast.Constant(key), node.left, node.comparators[0]], [])
        if isinstance(node.ops[0], ast.IsNot):
            probe = ast.UnaryOp(ast.Not(), probe)
        return ast.copy_location(probe, node)


class InstrumentedLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), self.path)
        marker = fullname.replace(".", "/")
        relative = marker + ("/__init__.py" if self.path.endswith("/__init__.py") else ".py")
        tree = self.probe(relative).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class SQLGlotFinder(importlib.abc.MetaPathFinder):
    def __init__(self, probe=Probe, packages=("sqlglot",)):
        self.probe = probe
        self.packages = packages

    def find_spec(self, fullname, path, target=None):
        if fullname.partition(".")[0] not in self.packages:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = InstrumentedLoader(fullname, spec.origin)
            spec.loader.probe = self.probe
        return spec


class TypeCoverage:
    def __init__(self):
        self.sites = {}
        self.observed = {}
        builtins._blast_operands = self.compare
        sys.meta_path.insert(0, SQLGlotFinder())

    def compare(self, site, left, right):
        pair = type(left), type(right)
        self.observed.setdefault(site, set()).add(pair)
        return left is right

    def flush(self, mask):
        for site, pairs in self.observed.items():
            if mask:
                table = self.sites.setdefault(site, {})
                for left, right in pairs:
                    key = type_category(left) + "|" + type_category(right)
                    table[key] = table.get(key, 0) | mask
        self.observed.clear()
