"""Compare real baseline calls at executor boundaries before scheduling an expensive query suite."""
import ast
import gzip
import hashlib
import json
from pathlib import Path

PREFIXES = ("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/optimizer/")
TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def digest(value):
    return hashlib.sha256(json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def evaluate(record):
    from sqlglot import parse_one, transpile, serde
    kind, value = record
    if kind == "parse":
        return parse_one(value).dump()
    if kind == "transpile":
        return transpile(value, write="duckdb")
    if kind == "python":
        from sqlglot.executor.python import Python
        return Python().generator(identify=True, comments=False).generate(serde.load(value))
    if kind == "optimize":
        from sqlglot.optimizer import optimize
        from sqlglot.schema import ensure_schema
        from tests.helpers import TPCDS_SCHEMA
        return optimize(serde.load(value), ensure_schema(TPCDS_SCHEMA),
                        leave_tables_isolated=True, dialect=None).dump()
    raise ValueError("unknown checkpoint")


def evaluate_calls(records, calls, costs, limit=3200):
    different = set()
    for i, owners in calls:
        inputs, expected = records[i]
        try:
            matches = digest(evaluate(inputs)) == expected
        except Exception:
            matches = False
        if not matches:
            different.update(owners)
            if sum(costs[q] for q in different) > limit:
                return None
    return sorted(different)


def local_expression_change(model, edited, lines):
    """Permit changes to operators and literals in existing function expressions."""
    def skeleton(node):
        if isinstance(node, ast.Constant):
            return ("literal", type(node.value).__name__)
        if isinstance(node, (ast.boolop, ast.cmpop, ast.operator)):
            return type(node).__bases__[0].__name__
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return skeleton(node.operand)
        if isinstance(node, ast.AST):
            return (type(node).__name__, tuple(skeleton(x) for _, x in ast.iter_fields(node)))
        if isinstance(node, list):
            return tuple(skeleton(x) for x in node)
        return node
    for line in lines:
        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.body[0].lineno <= line for n in model.scope(line)):
            return False
    return skeleton(model.tree) == skeleton(edited)


class Checkpoints:
    def __init__(self, filename):
        self.data = None
        self.filename = filename

    def plan(self, impact, edits):
        if not all(e.old == e.new and e.old.startswith(PREFIXES) and e.touched and not e.insertions for e in edits):
            return None
        if self.data is None:
            with gzip.open(self.filename, "rt") as stream:
                self.data = json.load(stream)
            masks = [int(m, 16) for m in self.data["masks"]]
            self.lines = {p: {int(n): masks[v] for n, v in rows.items()}
                          for p, rows in self.data["lines"].items()}
        bits = 0
        for e in edits:
            src = impact.model(e.old)
            if e.old not in self.lines or not local_expression_change(src, ast.parse(e.apply(src.text)), e.touched):
                return None
            for n in e.touched:
                bits |= impact.line_impact(e.old, src, n, rows=self.lines[e.old])
        if bits & 1 or not bits:
            return None
        selected = []
        while bits:
            bit = bits & -bits
            selected.append(bit.bit_length() - 2)
            bits ^= bit
        return selected


if __name__ == "__main__":
    import sys
    root, data, request, receipt = sys.argv[1:]
    sys.path.insert(0, root)
    with gzip.open(data, "rt") as stream:
        records = json.load(stream)
    calls, costs = json.loads(Path(request).read_bytes())
    result = evaluate_calls(records, calls, costs)
    Path(receipt).write_text("null" if result is None else json.dumps(result))
