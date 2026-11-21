import difflib
import json
from pathlib import Path
import tempfile
import unittest

from selector import Impact, parse_patch, schedule


def patch(path, old, new):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        "a/" + path, "b/" + path, n=1))


class DiffTests(unittest.TestCase):
    def test_multiple_files_and_hunks(self):
        base = "".join(f"x{i} = {i}\n" for i in range(10))
        new = base.replace("= 1\n", "= 14\n").replace("x6 = 6\n", "x6 = 6\nz = 0\n")
        edits = parse_patch(patch("a.py", base, new) + patch("b.py", "b=0\n", "b=2\n"))
        self.assertEqual(edits[0].apply(base), new)
        self.assertEqual(edits[0].touched, {2})
        self.assertEqual(edits[0].insertions, {7})
        self.assertEqual(edits[1].apply("b=0\n"), "b=2\n")

    def test_no_newline_marker(self):
        edit = parse_patch("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n")[0]
        self.assertEqual(edit.apply("a\n"), "b")

    def test_hunk_context_checked(self):
        with self.assertRaises(ValueError):
            parse_patch(patch("f", "x\n", "y\n"))[0].apply("z\n")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = "sqlglot/example.py"
        (self.root / "sqlglot").mkdir()
        self.base = 'def greet():\n    message = """hi\n# words\n"""\n    return message\n'
        (self.root / self.path).write_text(self.base)
        index = dict(tests=["t::A::one", "t::A::two"], masks=["0", "1", "2", "3"],
                     complete=3, files={"tests/fixtures/a.sql": 2}, dialects={}, expressions={},
                     lines={self.path: {"2": 1, "5": 1}})
        filename = self.root / "map.json"
        filename.write_text(json.dumps(index))
        tests = [{"id": t, "number": i, "file": "t"} for i, t in zip([42, 7], index["tests"])]
        self.model = Impact(self.root, filename, tests)

    def test_source_sensitive_comments_and_remapped_ids(self):
        before = self.base
        self.assertEqual(self.model.choose(patch(self.path, before, "# note\n" + before)), [])
        change = patch(self.path, before, before.replace("# words", "# new words"))
        self.assertEqual(self.model.choose(change), [42])
        fixture = patch("tests/fixtures/a.sql", "SELECT 1;\n", "SELECT 2;\n")
        self.assertEqual(set(self.model.choose(change + fixture)), {42, 7})

    def test_custom_equality_and_mixed_changes(self):
        base = "def check(x):\n    ans = x.value is None\n    return ans\n"
        (self.root / self.path).write_text(base)
        self.model.lines[self.path] = {2: 3, 3: 3}
        self.model.operands[f"{self.path}:2:10"] = {"identity|none": 1, "other|none": 2}
        new = base.replace("is None", "== None")
        self.assertEqual(self.model.choose(patch(self.path, base, new)), [7])
        self.assertEqual(set(self.model.choose(patch(self.path, base, new.replace("return ans", "return False")))),
                         {7, 42})

    def test_changed_import(self):
        new = "import missing_dependency\n" + self.base
        self.assertEqual(set(self.model.choose(patch(self.path, self.base, new))), {7, 42})

    def test_collection_and_unrecognized_diffs(self):
        broken = patch(self.path, self.base, "def nope(:\n")
        for change in (broken, "GIT binary patch", ""):
            self.assertEqual(set(self.model.choose(change)), {42, 7})


class WorkerTests(unittest.TestCase):
    def test_overhead_and_makespan(self):
        self.assertEqual(schedule([], {}), [])
        self.assertEqual(len(schedule([0, 1], {0: 300, 1: 700})), 1)
        plan = schedule([0, 1, 2], {0: 7000, 1: 6900, 2: 100})
        self.assertEqual(plan, [[0], [1, 2]])
        self.assertEqual(schedule([4, 2], {4: 0, 2: 0}), [[2, 4]])
