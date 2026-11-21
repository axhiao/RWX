"""Verify small candidate groups in an isolated checkout of the patched code."""
import concurrent.futures as futures
import pickle
import importlib
import json
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


class InlinePool(futures.Executor):
    """Keep fixture queries submitted to process pools in the tracing process."""
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        result = futures.Future()
        try:
            work, a, kw = pickle.loads(pickle.dumps((fn, args, kwargs)))
            value = work(*a, **kw)
            result.set_result(pickle.loads(pickle.dumps(value)))
        except Exception as error:
            result.set_exception(error)
        return result


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


@contextmanager
def checkout(source, edits):
    changes = {}
    for edit in edits:
        if (not edit.old.startswith("sqlglot/") or not edit.old.endswith(".py")
                or edit.old != edit.new):
            raise ValueError("verification requires modified production Python files")
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
        yield root, env


def child(script, root, env, extra, timeout):
    proc = subprocess.Popen([sys.executable, str(script), str(root), *map(str, extra)],
                            cwd=root, env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return proc.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        return False


def patched_state(source, edits):
    with checkout(source, edits) as (root, env):
        result = root.parent / "state.json"
        if child(Path(__file__).with_name("dialect_state.py"), root, env, [result], 2.5):
            return json.loads(result.read_bytes())
    return None


def changed_queries(source, edits, data, calls, costs, timeout):
    with checkout(source, edits) as (root, env):
        request, receipt = root.parent / "calls.json", root.parent / "matched"
        request.write_text(json.dumps([calls, costs]))
        if child(Path(__file__).with_name("checkpoints.py"), root, env,
                 [data.resolve(), request, receipt], timeout):
            return json.loads(receipt.read_bytes())
        return None


def verify(source, edits, nodes, cases, timeout):
    with checkout(source, edits) as (root, env):
        request, result = root.parent / "request.json", root.parent / "passes.json"
        request.write_text(json.dumps([nodes, cases]))
        if child(__file__, root, env, [request, result], timeout):
            return set(json.loads(result.read_bytes()))
    return set()


def all_pass(source, edits, test_ids, timeout):
    return verify(source, edits, test_ids, {}, timeout) == set(test_ids)


def run_tests(root, request, receipt):
    sys.path.insert(0, root)
    nodes, cases = json.loads(Path(request).read_bytes())
    # Preserve the serialization boundary of upstream pool tasks. Using an
    # inline executor removes process startup from this local verification.
    futures.ProcessPoolExecutor = InlinePool
    groups = {}
    for node in nodes:
        file, cls, method = node.split("::")
        try:
            mod = importlib.import_module(file[:-3].replace("/", "."))
            if hasattr(mod, "setUpModule") or hasattr(mod, "tearDownModule"):
                continue
            groups.setdefault(getattr(mod, cls), []).append((node, method))
        except (ImportError, AttributeError):
            continue
    passed = []
    for cls, methods in groups.items():
        group_passes = []
        try:
            cls.setUpClass()
            try:
                for node, method in methods:
                    selection = cases.get(node)
                    fixture_module = None
                    field = None
                    if isinstance(selection, dict):
                        fixture_module = sys.modules[cls.__module__]
                        loader = fixture_module.load_sql_fixture_pairs
                        def load_subset(filename):
                            pairs = list(loader(filename))
                            if filename not in selection:
                                return pairs
                            count, indexes = selection[filename]
                            if len(pairs) != count:
                                raise ValueError("fixture index does not match the checkout")
                            return [pairs[i] for i in indexes]
                        fixture_module.load_sql_fixture_pairs = load_subset
                    elif selection:
                        field, ids = selection
                    if field:
                        original = getattr(cls, field)
                        setattr(cls, field, [original[i] for i in ids])
                    try:
                        result = unittest.TestResult()
                        result.failfast = True
                        cls(method).run(result)
                        if (result.testsRun == 1 and result.wasSuccessful() and not
                                (result.skipped or result.expectedFailures)):
                            group_passes.append(node)
                    finally:
                        if fixture_module:
                            fixture_module.load_sql_fixture_pairs = loader
                        if field:
                            setattr(cls, field, original)
            finally:
                cls.tearDownClass()
                cls.doClassCleanups()
            if not getattr(cls, "tearDown_exceptions", []):
                passed.extend(group_passes)
        except Exception:
            continue
    Path(receipt).write_text(json.dumps(passed))


if __name__ == "__main__":
    run_tests(*sys.argv[1:])
