"""Bound a source edit by the innermost baseline expressions that it changes."""
import ast
import gzip
import json


def location(node):
    return f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}:{type(node).__name__}"


class ExpressionCoverage:
    def __init__(self, filename, names):
        with gzip.open(filename, "rt") as stream:
            data = json.load(stream)
        if names != data["tests"]:
            raise ValueError("expression index uses different test IDs")
        self.masks = [int(m, 16) for m in data["masks"]]
        self.files = data["files"]
        self.includes_imports = data.get("includes_imports", False)
        self.uncertain = self.masks[data["uncertain"]]

    def impact(self, path, old, new, lines):
        sites = self.files.get(path)
        if not sites:
            return None

        class UnsupportedChange(Exception):
            pass

        def compare(a, b):
            if isinstance(a, ast.AST) and hasattr(a, "lineno") and not any(
                    a.lineno <= n <= a.end_lineno for n in lines):
                if isinstance(b, ast.AST) and ast.dump(a) == ast.dump(b):
                    return 0
                raise UnsupportedChange
            own = None
            if isinstance(a, ast.expr):
                index = sites.get(location(a))
                if index is not None:
                    own = self.masks[index]
            if type(a) is not type(b):
                for group in (ast.boolop, ast.cmpop, ast.operator, ast.unaryop):
                    if isinstance(a, group) and isinstance(b, group):
                        return None
                if isinstance(a, ast.UnaryOp) and isinstance(a.op, ast.Not) and isinstance(b, ast.expr):
                    if ast.dump(a.operand) == ast.dump(b):
                        return own
                if isinstance(b, ast.UnaryOp) and isinstance(b.op, ast.Not) and isinstance(a, ast.expr):
                    if ast.dump(a) == ast.dump(b.operand):
                        return own
                raise UnsupportedChange
            if isinstance(a, ast.Constant):
                return 0 if ast.dump(a) == ast.dump(b) else own
            if isinstance(a, ast.AST):
                pairs = ((getattr(a, name), getattr(b, name)) for name in a._fields)
            elif isinstance(a, list) and len(a) == len(b):
                pairs = zip(a, b)
            elif a == b:
                return 0
            else:
                raise UnsupportedChange
            mask = 0
            for left, right in pairs:
                affected = compare(left, right)
                if affected is None:
                    mask = None
                elif mask is not None:
                    mask |= affected
            return own if mask is None else mask

        try:
            value = compare(old, new)
        except UnsupportedChange:
            return None
        return None if value is None else value | self.uncertain
