import ast
import json
from pathlib import Path
import tempfile
import unittest

from replay import all_pass
from selector import FileEdit, Source, parse_patch
from test_selector import patch
from value_profiles import Profiles


class ValueProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "example.py"

    def impact(self, source, changed, entries=None, scalars=None):
        self.file.write_text(source)
        f = self.root / "profile.json"
        f.write_text(json.dumps(dict(tests=[], complete=0, masks=["0", "1", "2"], entries=entries or {},
                                     scalars=scalars or {})))
        profile = Profiles(f, [], self.root)
        edit, = parse_patch(patch("example.py", source, changed))
        return profile.impact("example.py", Source(self.file), ast.parse(changed), edit.touched, {})

    def test_dictionary_value_requires_same_keys_and_surroundings(self):
        src = 'class C:\n    table = {"x": 1, "y": 2}\n'
        entries = {"example.py:2:13": 2}
        self.assertEqual(self.impact(src, src.replace(": 1", ": 3"), entries), 2)
        self.assertIsNone(self.impact(src, src.replace('"x": 1', '"z": 3'), entries))
        self.assertIsNone(self.impact(src, src.replace(": 1", ": f()"), entries))

    def test_comparison_boundary_and_unknown_type(self):
        src = "def f(x):\n    return x < 5\n"
        changed = src.replace("<", "<=")
        self.assertEqual(self.impact(src, changed, scalars={"example.py:2:11": {"4": 1, "5": 2}}), 2)
        self.assertEqual(self.impact(src, changed, scalars={"example.py:2:11": {"4": 1, "?": 2}}), 2)

    def test_live_run_observes_patch_and_subtest_failures(self):
        (self.root / "sqlglot").mkdir()
        (self.root / "sqlglot/__init__.py").touch()
        src = "value = 1\n"
        (self.root / "sqlglot/feature.py").write_text(src)
        (self.root / "tests").mkdir()
        (self.root / "tests/__init__.py").touch()
        (self.root / "tests/spec.py").write_text(
            "import unittest\nfrom sqlglot.feature import value\n"
            "class T(unittest.TestCase):\n    def test_value(self):\n"
            "        with self.subTest():\n            self.assertEqual(value, 2)\n")
        for value, expected in [(2, True), (3, False)]:
            edits = parse_patch(patch("sqlglot/feature.py", src, f"value = {value}\n"))
            self.assertEqual(all_pass(self.root, edits, ["tests/spec.py::T::test_value"], 3), expected)
            self.assertEqual((self.root / "sqlglot/feature.py").read_text(), src)
