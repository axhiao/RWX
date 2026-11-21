"""Measure dependencies on a clean SQLGlot checkout with CPython 3.13."""
import argparse
import ast
import concurrent.futures as futures
from contextlib import redirect_stdout
import importlib
import inspect
import json
import os
import pickle
from pathlib import Path
import subprocess
import sys
import unittest
from collections import defaultdict
from functools import cache

from operand_types import TypeCoverage

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


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


class Recorder:
    def __init__(self, root, profiles):
        self.profiles = profiles
        self.prefix = str(root) + "/"
        self.lines = defaultdict(dict)
        self.files = {}
        self.dialects = {}
        self.expressions = {}
        self.active = 0
        self.seen_types = set()
        self.seen_dialects = set()
        self.tool = sys.monitoring.COVERAGE_ID
        mon = sys.monitoring
        mon.use_tool_id(self.tool, "blast-radius")
        mon.register_callback(self.tool, mon.events.PY_START, self.start_code)
        mon.register_callback(self.tool, mon.events.LINE, self.line)
        mon.set_events(self.tool, mon.events.PY_START)
        sys.addaudithook(self.audit)

    @cache
    def relative(self, filename):
        if filename.startswith(self.prefix):
            return filename[len(self.prefix):]
        return None

    def start_code(self, code, offset):
        if self.relative(code.co_filename) and code.co_flags & inspect.CO_OPTIMIZED:
            sys.monitoring.set_local_events(self.tool, code, sys.monitoring.events.LINE)
        return sys.monitoring.DISABLE

    def line(self, code, line):
        if self.active:
            rows = self.lines[self.relative(code.co_filename)]
            rows[line] = rows.get(line, 0) | self.active
        return sys.monitoring.DISABLE

    def audit(self, event, args):
        if self.active and event == "open" and isinstance(args[0], str):
            # Opening a module to compile/import it does not make ordinary
            # comments observable. Explicit open()/inspect calls still count.
            caller = sys._getframe(1).f_globals.get("__name__")
            if caller in {"importlib._bootstrap_external", "_frozen_importlib_external"}:
                return
            path = os.path.abspath(args[0])
            rel = self.relative(path)
            if rel and "__pycache__" not in rel and rel != ".":
                self.files[rel] = self.files.get(rel, 0) | self.active

    def begin(self, mask):
        self.profiles.flush(0)
        self.active = mask
        self.seen_types.clear()
        self.seen_dialects.clear()
        sys.monitoring.restart_events()

    def end(self):
        self.profiles.flush(self.active)
        for cls in self.seen_types:
            for base in cls.__mro__:
                if base.__module__.startswith("sqlglot.expressions"):
                    name = base.__name__
                    self.expressions[name] = self.expressions.get(name, 0) | self.active
        for cls in self.seen_dialects:
            for base in cls.__mro__:
                if base.__module__.startswith("sqlglot.dialects."):
                    name = base.__module__.rsplit(".", 1)[-1]
                    self.dialects[name] = self.dialects.get(name, 0) | self.active
        self.active = 0


def attribute_reads(root, record):
    """Cross-reference simple configuration fields with covered read sites.

    A metaclass or a class body can turn a field into another dispatch table
    during import. In that case ordinary reads cannot bound its consumers.
    """
    fields = defaultdict(set)
    opaque = set()
    for folder in ("sqlglot", "tests"):
        for file in sorted((root / folder).rglob("*.py")):
            path = file.relative_to(root).as_posix()
            tree = ast.parse(file.read_bytes())
            parents = {child: node for node in ast.walk(tree)
                       for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value.isidentifier() and node.value.isupper():
                        opaque.add(node.value)
                    continue
                if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                        and node.attr.isupper()):
                    continue
                enclosing = node
                while enclosing in parents and not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    enclosing = parents[enclosing]
                if isinstance(enclosing, ast.Module) or getattr(enclosing, "name", "") in (
                    "__new__", "__init_subclass__", "__set_name__",
                ):
                    opaque.add(node.attr)
                    continue
                # A multiline expression may report the statement start, the
                # attribute's source line, or the line containing its receiver.
                sites = fields[node.attr]
                sites.update((path, n) for n in range(node.lineno, node.end_lineno + 1))
                parent = node
                while parent in parents and not isinstance(parent, ast.stmt):
                    parent = parents[parent]
                if isinstance(parent, ast.stmt):
                    sites.add((path, parent.lineno))
    reads = {}
    for name, sites in fields.items():
        if name in opaque:
            continue
        mask = 0
        for path, line in sites:
            mask |= record.lines.get(path, {}).get(line, 0)
        if mask:
            reads[name] = mask
    return reads


def trace_import(source, module):
    # Start a fresh interpreter per test module so importlib's module cache
    # cannot credit initialization work only to the first test in the batch.
    root = Path(source).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    with redirect_stdout(sys.stderr):
        recorder = Recorder(root, None)
        recorder.active = 1
        importlib.import_module(module)
        recorder.active = 0
    print(json.dumps({path: list(rows) for path, rows in recorder.lines.items()}))


def import_coverage(root, groups):
    masks = defaultdict(int)
    for (path, _), tests in groups.items():
        masks[path] |= sum(1 << idx for idx, _ in tests)
    lines = defaultdict(dict)
    for file, mask in masks.items():
        module = file[:-3].replace("/", ".")
        result = subprocess.run([sys.executable, __file__, "--trace-import", str(root), module],
                                capture_output=True, check=False)
        if result.returncode:
            raise RuntimeError(f"could not trace import of {module}: {result.stderr.decode()[-1000:]}")
        for path, locations in json.loads(result.stdout).items():
            rows = lines[path]
            for location in locations:
                rows[location] = rows.get(location, 0) | mask
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    names = sorted(row["id"] for row in json.loads(args.catalog.read_text())["tests"])
    head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip()
    if head != PIN or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode:
        parser.error(f"expected a clean tracked tree at {PIN}")
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(root))
    # Import from the checkout before enabling tracing or installing probes.
    profiles = TypeCoverage()
    from sqlglot import exp
    from sqlglot.dialects.dialect import Dialect
    futures.ProcessPoolExecutor = InlinePool
    groups = defaultdict(list)
    for idx, node in enumerate(names):
        file, cls, method = node.split("::")
        groups[file, cls].append((idx, method))

    classes = {}
    for file, cls in groups:
        module = importlib.import_module(file[:-3].replace("/", "."))
        classes[file, cls] = getattr(module, cls)

    record = Recorder(root, profiles)
    orig_init = exp.Expr.__init__
    def record_init(self, **kwargs):
        record.seen_types.add(type(self))
        orig_init(self, **kwargs)
    exp.Expr.__init__ = record_init

    orig_get = Dialect.get_or_raise.__func__
    def record_get(cls, dialect):
        d = orig_get(cls, dialect)
        record.seen_dialects.add(type(d))
        return d
    Dialect.get_or_raise = classmethod(record_get)

    complete = 0
    for key, methods in groups.items():
        cls = classes[key]
        class_mask = sum(1 << idx for idx, _ in methods)
        record.begin(class_mask)
        try:
            cls.setUpClass()
        finally:
            record.end()
        for idx, method in methods:
            record.begin(1 << idx)
            try:
                result = unittest.TestResult()
                cls(method).run(result)
            finally:
                record.end()
            if result.wasSuccessful() and not result.skipped:
                complete |= 1 << idx
            else:
                details = result.failures + result.errors + result.skipped
                print(f"incomplete {names[idx]}: {str(details[0][1])[-300:]}", file=sys.stderr)
        record.begin(class_mask)
        try:
            cls.tearDownClass()
            cls.doClassCleanups()
        finally:
            record.end()
        print(f"indexed {key[1]} ({len(methods)} tests)", file=sys.stderr, flush=True)

    # Intern repeated bitsets. Thousands of adjacent lines share a test set.
    masks = {}
    def intern(mask):
        if mask not in masks:
            masks[mask] = len(masks)
        return masks[mask]
    def pack(table):
        return {key: intern(mask) for key, mask in sorted(table.items())}
    field_reads = pack(attribute_reads(root, record))
    cold = {path: pack(rows) for path, rows in sorted(import_coverage(root, groups).items())}
    lines = {path: pack(rows) for path, rows in sorted(record.lines.items())}
    data = dict(commit=PIN, tests=names, lines=lines, files=pack(record.files),
                dialects=pack(record.dialects), expressions=pack(record.expressions),
                imports=cold, attributes=field_reads, operands={k: pack(v) for k, v in sorted(profiles.sites.items())},
                complete=intern(complete), masks=[format(mask, "x") for mask in masks])
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".tmp")
    temp.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    temp.replace(output)
    print(f"{complete.bit_count()}/{len(names)} complete, {output.stat().st_size:,} bytes", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--trace-import"]:
        trace_import(*sys.argv[2:])
    else:
        main()
