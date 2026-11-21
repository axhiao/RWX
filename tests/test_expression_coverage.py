import ast
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from comparison_relations import changed_mask
from expression_coverage import ExpressionCoverage, location
from query_replay import QueryCoverage


class ExpressionTests(unittest.TestCase):
    def test_guarded_comparison_and_unrelated_identifier_edit(self):
        source = "def match(ok, x, y):\n    return ok and x == y\n"
        tree = ast.parse(source)
        comparison = next(n for n in ast.walk(tree) if isinstance(n, ast.Compare))
        outer = next(n for n in ast.walk(tree) if isinstance(n, ast.BoolOp))
        data = {"tests": [], "uncertain": 0, "masks": ["0", "1", "f"],
                "files": {"f.py": {location(comparison): 1, location(outer): 2}}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "map.gz"
            path.write_bytes(gzip.compress(json.dumps(data).encode()))
            index = ExpressionCoverage(path, [])
        def bound(text):
            return index.impact("f.py", tree, ast.parse(text), {2})
        self.assertEqual(bound(source.replace("==", "!=")), 1)
        self.assertEqual(bound(source.replace("and", "or")), 15)
        self.assertIsNone(bound(source.replace("== y", "== z")))
        self.assertIsNone(bound(source + "z = 0\n"))

    def test_unordered_and_custom_values_stay_conservative(self):
        states = {"0<": 0, "0=": 1, "0unordered": 2, "?": 3}
        masks = [1, 2, 4, 8]
        self.assertEqual(changed_mask(states, ast.Lt(), ast.LtE(), masks), 2 | 8)
        self.assertEqual(changed_mask(states, ast.Eq(), ast.Is(), masks), 2 | 8)
        self.assertEqual(changed_mask({"1unordered": 2}, ast.Is(), ast.Eq(), masks), 4)

    def test_parser_slice_rejects_changed_setup_and_expensive_call(self):
        index = QueryCoverage.__new__(QueryCoverage)
        index.parser_calls = [{"cost": 2}, {"cost": 9000}]
        from selector import FileEdit
        edits = [FileEdit("sqlglot/parser.py", "sqlglot/parser.py")]
        with patch.object(index, "affected", return_value=2):
            self.assertEqual(index.parser_plan(None, edits)["live"], [0])
        for bits in (1, 4):
            with patch.object(index, "affected", return_value=bits):
                self.assertIsNone(index.parser_plan(None, edits))
