"""Replay individual TPC fixture cases when only executable function bodies change."""
import ast
import gzip
import json


class QueryCoverage:
    def __init__(self, path):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        self.methods = data.get("methods", {})
        self.ranges = data.get("ranges", {})
        masks = [int(m, 16) for m in data["masks"]]
        self.lines = {p: {int(n): masks[v] for n, v in rows.items()} for p, rows in data["lines"].items()}

    def plan(self, impact, edits, candidates):
        selected = {node: node.rsplit("::", 1)[-1] for node in candidates
                    if node.startswith("tests/test_executor.py::TestExecutor::")
                    and node.rsplit("::", 1)[-1] in self.methods}
        if not selected:
            return {}
        affected = self.affected(impact, edits)
        if affected is None:
            return {}
        if affected & 1:
            return {}
        plans = {}
        for node, method in selected.items():
            field, offset, costs = self.methods[method]
            indexes = [i for i in range(len(costs)) if affected & (1 << (offset + i))]
            if indexes and sum(costs[i] for i in indexes) <= 2400:
                plans[node] = [field, indexes]
        return plans

    def affected(self, impact, edits):
        affected = 0
        for edit in edits:
            if (edit.old != edit.new or not edit.old.startswith("sqlglot/") or not edit.old.endswith(".py")
                    or not edit.touched or edit.insertions):
                return None
            model = impact.model(edit.old)
            new = ast.parse(edit.apply(model.text))
            # A new declaration, decorator or default could change import-time state.
            for tree, lines in ((model.tree, edit.touched), (new, edit.new_lines)):
                bodies = [(n.body[0].lineno, n.end_lineno) for n in ast.walk(tree)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body]
                if any(not any(start <= n <= end for start, end in bodies) for n in lines):
                    return None
            for line in edit.touched:
                affected |= impact.line_impact(edit.old, model, line, rows=self.lines.get(edit.old, {}))
        return affected

    def fixture_plan(self, impact, edits, candidates):
        selected = {node: node.rsplit("::", 1)[-1] for node in candidates
                    if node.startswith("tests/test_optimizer.py::TestOptimizer::")
                    and node.rsplit("::", 1)[-1] in self.ranges}
        if not selected:
            return {}
        affected = self.affected(impact, edits)
        if affected is None:
            return {}
        result = {}
        for node, method in selected.items():
            ranges = self.ranges[method]
            keep = {file: [size, [i for i in range(size) if affected & (1 << (start + i))]]
                    for file, (start, size) in ranges.items()}
            size = sum(row[0] for row in keep.values())
            count = sum(len(row[1]) for row in keep.values())
            if count <= 40 and count * 3 <= size:
                result[node] = keep
        return result
