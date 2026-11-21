"""Verify small candidate groups in an isolated checkout of the patched code."""
import concurrent.futures as futures
from contextlib import contextmanager
import gzip
import importlib
import json
import os
from pathlib import Path
import pickle
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


_WARMED = False
_CHECKPOINT_RECORDS = {}
_ENV_DEFAULTS = {
    "SKIP_INTEGRATION": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHON_CPU_COUNT": "2",
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
}


def warmup():
    """Pre-import heavy non-sqlglot dependencies once in the parent before forking."""
    global _WARMED
    if _WARMED:
        return
    _WARMED = True
    for key, value in _ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)
    for mod in ("dateutil", "dateutil.parser", "pytz", "typing_extensions",
                "numpy", "pandas", "duckdb"):
        try:
            importlib.import_module(mod)
        except Exception:
            pass


def _checkpoint_records(path):
    key = str(Path(path).resolve())
    records = _CHECKPOINT_RECORDS.get(key)
    if records is None:
        with gzip.open(key, "rt") as stream:
            records = json.load(stream)
        _CHECKPOINT_RECORDS[key] = records
    return records


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
               **_ENV_DEFAULTS}
        yield root, env


def _kill_and_reap(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _fork_run(root, fn, timeout):
    """Run fn() inside a clean forked child with CoW-inherited libraries and strict timeout."""
    sys.stdout.flush()
    sys.stderr.flush()
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            os.close(r_fd)
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            if devnull > 2:
                os.close(devnull)
            for key, value in _ENV_DEFAULTS.items():
                os.environ[key] = value
            os.chdir(root)
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    sys.modules.pop(name, None)
            sys.path.insert(0, str(root))
            importlib.invalidate_caches()
            futures.ProcessPoolExecutor = InlinePool
            payload = json.dumps(fn(), separators=(",", ":")).encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(w_fd, view)
                view = view[written:]
            os.close(w_fd)
            os._exit(0)
        except BaseException:
            try:
                os.close(w_fd)
            except OSError:
                pass
            os._exit(1)

    os.close(w_fd)
    deadline = time.monotonic() + max(0.05, timeout)
    chunks = []
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([r_fd], [], [], remaining)
            if not ready:
                timed_out = True
                break
            chunk = os.read(r_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(r_fd)

    if timed_out:
        _kill_and_reap(pid)
        return False, None

    while True:
        done_pid, status = os.waitpid(pid, os.WNOHANG)
        if done_pid == pid:
            break
        if time.monotonic() >= deadline:
            _kill_and_reap(pid)
            return False, None
        time.sleep(0.002)

    if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
        return False, None
    try:
        return True, json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        return False, None


def child(script, root, env, extra, timeout):
    proc = subprocess.Popen([sys.executable, str(script), str(root), *map(str, extra)],
                            cwd=root, env=env, start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return proc.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        _kill_and_reap(proc.pid)
        return False


def _run_tests_in_process(nodes, cases):
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
    return passed


def patched_state(source, edits):
    with checkout(source, edits) as (root, env):
        if hasattr(os, "fork"):
            import dialect_state
            ok, data = _fork_run(root, dialect_state.state, 2.5)
            return data if ok else None
        result = root.parent / "state.json"
        if child(Path(__file__).with_name("dialect_state.py"), root, env, [result], 2.5):
            return json.loads(result.read_bytes())
    return None


def changed_queries(source, edits, data, calls, costs, timeout):
    with checkout(source, edits) as (root, env):
        if hasattr(os, "fork"):
            import checkpoints
            records = _checkpoint_records(data)
            ok, result = _fork_run(root, lambda: checkpoints.evaluate_calls(records, calls, costs), timeout)
            return result if ok else None
        request, receipt = root.parent / "calls.json", root.parent / "matched"
        request.write_text(json.dumps([calls, costs]))
        if child(Path(__file__).with_name("checkpoints.py"), root, env,
                 [data.resolve(), request, receipt], timeout):
            return json.loads(receipt.read_bytes())
        return None


def verify_commit(source, edits, nodes, cases, timeout, tpcds_check=None):
    """Run optional TPC-DS checkpoint filtering and test verification in a single forked child."""
    with checkout(source, edits) as (root, env):
        if hasattr(os, "fork"):
            records = _checkpoint_records(tpcds_check[0]) if tpcds_check else None
            def task():
                local_cases = dict(cases)
                run_nodes = list(nodes)
                passed = []
                if tpcds_check is not None:
                    import checkpoints
                    _, calls, costs, tpcds_node, tpcds_field = tpcds_check
                    rows = checkpoints.evaluate_calls(records, calls, costs)
                    if rows == []:
                        passed.append(tpcds_node)
                        run_nodes = [n for n in run_nodes if n != tpcds_node]
                    elif rows:
                        local_cases[tpcds_node] = [tpcds_field, rows]
                    else:
                        run_nodes = [n for n in run_nodes if n != tpcds_node]
                if run_nodes:
                    passed.extend(_run_tests_in_process(run_nodes, local_cases))
                return passed
            ok, result = _fork_run(root, task, timeout)
            return set(result) if ok and result is not None else set()

        local_cases = dict(cases)
        run_nodes = list(nodes)
        passed = set()
        if tpcds_check is not None:
            data, calls, costs, tpcds_node, tpcds_field = tpcds_check
            rows = changed_queries(source, edits, data, calls, costs, timeout)
            if rows == []:
                passed.add(tpcds_node)
                run_nodes = [n for n in run_nodes if n != tpcds_node]
            elif rows:
                local_cases[tpcds_node] = [tpcds_field, rows]
            else:
                run_nodes = [n for n in run_nodes if n != tpcds_node]
        if not run_nodes:
            return passed
        request, result = root.parent / "request.json", root.parent / "passes.json"
        request.write_text(json.dumps([run_nodes, local_cases]))
        if child(__file__, root, env, [request, result], timeout):
            passed.update(json.loads(result.read_bytes()))
        return passed


def verify(source, edits, nodes, cases, timeout):
    return verify_commit(source, edits, nodes, cases, timeout)


def all_pass(source, edits, test_ids, timeout):
    return verify(source, edits, test_ids, {}, timeout) == set(test_ids)


def run_tests(root, request, receipt):
    sys.path.insert(0, root)
    nodes, cases = json.loads(Path(request).read_bytes())
    # Preserve the serialization boundary of upstream pool tasks. Using an
    # inline executor removes process startup from this local verification.
    futures.ProcessPoolExecutor = InlinePool
    passed = _run_tests_in_process(nodes, cases)
    Path(receipt).write_text(json.dumps(passed))


if __name__ == "__main__":
    run_tests(*sys.argv[1:])
