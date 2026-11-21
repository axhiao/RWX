import ast
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from checkpoints import local_expression_change
from dialect_state import StateCoverage, eligible
from replay import verify, verify_commit, warmup
from selector import Source, parse_patch
from test_selector import patch


class ReplayPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_value_membership_length_and_unknown_key_are_distinct(self):
        data = {"tests": [], "masks": ["0", "1", "2", "4", "8", "10"],
                "base": {"D.table": {"'x'": "'old'"}}, "complete": 0,
                "reads": {"D.table": {"get'x'": 1, "in'x'": 2, "in'y'": 3, "len": 4, "keys": 5}}}
        f = self.root / "state.gz"
        f.write_bytes(gzip.compress(json.dumps(data).encode()))
        index = StateCoverage(f, [])
        self.assertEqual(index.impact({"D.table": {"'x'": "'new'"}}), 1)
        self.assertEqual(index.impact({"D.table": {"'x'": "'old'", "'y'": "''"}}), 4 | 8 | 16)
        self.assertIsNone(index.impact({}))

    def test_declaration_requires_complete_edit(self):
        f = self.root / "d.py"
        base = "class D:\n    TIME_MAPPING = {'a': 'X'}\n    enabled = True\n"
        f.write_text(base)
        changed = ast.parse(base.replace("'X'", "'Y'"))
        self.assertTrue(eligible(Source(f), changed, {2}))
        self.assertTrue(eligible(Source(f), changed, {2}))
        other = ast.parse(base.replace("'X'", "'Y'").replace('True', 'False'))
        self.assertFalse(eligible(Source(f), other, {2, 3}))

    def test_checkpoint_rejects_new_calls_and_defaults(self):
        f = self.root / "test.py"
        base = "def f(x=1):\n    return len(x) == 2\n"
        f.write_text(base)
        model = Source(f)
        self.assertTrue(local_expression_change(model, ast.parse(base.replace("==", "!=")), {2}))
        self.assertFalse(local_expression_change(model, ast.parse(base.replace("len", "max")), {2}))
        self.assertFalse(local_expression_change(model, ast.parse(base.replace("x=1", "x=2")), {1}))

    def test_class_fixtures_and_skips_are_part_of_pass_receipt(self):
        for directory in ("tests", "sqlglot"):
            (self.root / directory).mkdir()
            (self.root / directory / "__init__.py").touch()
        (self.root / "sqlglot/item.py").write_text("value = 1\n")
        spec = self.root / "tests/t.py"
        spec.write_text(
            "import unittest\nfrom sqlglot.item import value\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self): self.assertEqual(value, 2)\n"
            "    def test_bad(self): self.fail()\n"
            "    def test_skip(self): self.skipTest('skip')\n")
        edits = parse_patch(patch("sqlglot/item.py", "value = 1\n", "value = 2\n"))
        ids = ["tests/t.py::T::test_" + name for name in ("ok", "bad", "skip")]
        self.assertEqual(verify(self.root, edits, ids, {}, 3), {ids[0]})
        spec.write_text(spec.read_text() + "    @classmethod\n    def tearDownClass(cls): raise RuntimeError()\n")
        self.assertEqual(verify(self.root, edits, ids, {}, 3), set())

    def test_fork_timeout_and_stdout_isolation(self):
        warmup()
        for directory in ("tests", "sqlglot"):
            (self.root / directory).mkdir()
            (self.root / directory / "__init__.py").touch()
        (self.root / "sqlglot/item.py").write_text("value = 1\n")
        (self.root / "tests/t.py").write_text(
            "import time, unittest\nfrom sqlglot.item import value\n"
            "class T(unittest.TestCase):\n"
            "    def test_noisy(self):\n"
            "        print('SHOULD_NOT_LEAK_TO_STDOUT', flush=True)\n"
            "        self.assertEqual(value, 2)\n"
            "    def test_hang(self):\n"
            "        time.sleep(5)\n")
        edits = parse_patch(patch("sqlglot/item.py", "value = 1\n", "value = 2\n"))
        self.assertEqual(verify_commit(self.root, edits, ["tests/t.py::T::test_noisy"], {}, 2.0),
                         {"tests/t.py::T::test_noisy"})
        self.assertEqual(verify_commit(self.root, edits, ["tests/t.py::T::test_hang"], {}, 0.15), set())


if __name__ == "__main__":
    unittest.main()
