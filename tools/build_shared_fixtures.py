"""Find class fixtures loaded into lists and trace which executor methods consume them."""
import argparse
import ast
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from build_index import InlinePool, PIN


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "catalog", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    names = [r["id"] for r in json.loads(args.catalog.read_bytes())["tests"]
             if r["file"] == "tests/test_executor.py"]
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected the pristine pinned checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from tests.test_executor import TestExecutor
    tree = ast.parse((root / "tests/test_executor.py").read_bytes())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == TestExecutor.__name__)
    setup = next(n for n in cls.body if getattr(n, "name", "") == "setUpClass")
    fields = {}
    for stmt in setup.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target, value = stmt.targets[0], stmt.value
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.value.id == "cls" and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name) and value.func.id == "list" and len(value.args) == 1):
            continue
        call = value.args[0]
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "load_sql_fixture_pairs" and len(call.args) == 1
                and isinstance(call.args[0], ast.Constant)):
            fields[target.attr] = call.args[0].value

    # Aliases or derived state computed in setup would need their own trace.
    for node in ast.walk(setup):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            fields.pop(node.attr, None)
    TestExecutor.setUpClass()
    touched = set()
    class Field:
        def __init__(self, attr, value):
            self.attr = attr
            self.value = value

        def __get__(self, instance, owner=None):
            touched.add(self.attr)
            return self.value

    result = {path: {} for path in fields.values()}
    for field in fields:
        setattr(TestExecutor, field, Field(field, getattr(TestExecutor, field)))
    for node in names:
        touched.clear()
        outcome = unittest.TestResult()
        TestExecutor(node.rsplit("::", 1)[1]).run(outcome)
        if outcome.wasSuccessful() and not outcome.skipped:
            for attr, path in fields.items():
                result[path][node] = attr if attr in touched else None
    TestExecutor.tearDownClass()
    TestExecutor.doClassCleanups()
    output.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
