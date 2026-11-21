"""Verify small candidate groups in an isolated checkout of the patched code."""
import importlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


def overlay(base, target, changes):
    """Build only the changed directories, sharing other files read-only."""
    target.mkdir()
    for item in base.iterdir():
        if item.name == ".git":
            continue
        if item.name == "__pycache__" and any(name.endswith(".py") for name in changes):
            # Never reuse bytecode for a modified file, including same-size edits.
            cache = target / item.name
            cache.mkdir()
            for pyc in item.iterdir():
                if pyc.name.partition(".")[0] + ".py" not in changes:
                    (cache / pyc.name).symlink_to(pyc)
            continue
        dest = target / item.name
        if item.name not in changes:
            dest.symlink_to(item, target_is_directory=item.is_dir())
        elif isinstance(changes[item.name], dict):
            overlay(item, dest, changes[item.name])
        else:
            dest.write_text(changes[item.name], encoding="utf-8")


def all_pass(source, edits, test_ids, timeout):
    changes = {}
    for edit in edits:
        if (not edit.old.startswith("sqlglot/") or not edit.old.endswith(".py")
                or edit.old != edit.new):
            return False
        tree = changes
        *dirs, name = Path(edit.old).parts
        for directory in dirs:
            tree = tree.setdefault(directory, {})
        tree[name] = edit.apply((source / edit.old).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "src"
        overlay(source.resolve(), root, changes)
        env = {**os.environ, "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
               "SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_CPU_COUNT": "2",
               "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        receipt = Path(temp) / "passed"
        proc = subprocess.Popen([sys.executable, __file__, str(root), str(receipt), *test_ids],
                                cwd=root, env=env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return proc.wait(timeout=timeout) == 0 and receipt.exists()
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return False


def run_tests(root, receipt, nodes):
    sys.path.insert(0, root)
    suite = unittest.TestSuite()
    for node in nodes:
        file, cls, method = node.split("::")
        mod = importlib.import_module(file[:-3].replace("/", "."))
        suite.addTest(getattr(mod, cls)(method))
    result = unittest.TestResult()
    result.failfast = True
    suite.run(result)
    if (result.testsRun == len(nodes) and result.wasSuccessful() and
            not (result.skipped or result.expectedFailures)):
        Path(receipt).touch()


if __name__ == "__main__":
    run_tests(sys.argv[1], sys.argv[2], sys.argv[3:])
