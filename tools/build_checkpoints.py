"""Capture baseline executor call inputs, outputs, and source coverage without applying changes."""
import argparse
import concurrent.futures
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from build_index import InlinePool, PIN, Recorder

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checkpoints import PREFIXES, digest, evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root, directory = args.source.resolve(), args.directory.resolve()
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected a clean pinned checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    class Trace(Recorder):
        def start_code(self, code, offset):
            path = self.relative(code.co_filename)
            if path and path.startswith(PREFIXES):
                return super().start_code(code, offset)
            return sys.monitoring.DISABLE
    trace = Trace(root, None)
    trace.active = 1
    from tests import test_executor as upstream
    import sqlglot.executor as engine
    from sqlglot.generators.python import PythonGenerator
    records, seen, undo, owners = [], {}, [], []
    case_number = None
    def install(owner, name, kind, encode, decode):
        original = getattr(owner, name)
        def call(*args, **kwargs):
            if trace.active != 1:
                return original(*args, **kwargs)
            payload = encode(args, kwargs)
            if payload is None:
                return original(*args, **kwargs)
            inputs = [kind, payload]
            key = digest(inputs)
            i = seen.get(key)
            if i is None:
                i = seen[key] = len(records)
                records.append([inputs, None])
                owners.append(set())
            owners[i].add(case_number)
            trace.active = 1 << (i + 1)
            sys.monitoring.restart_events()
            try:
                value = original(*args, **kwargs)
            finally:
                trace.active = 1
                sys.monitoring.restart_events()
            observed = digest(decode(value))
            # An input with multiple outputs cannot serve as a checkpoint.
            expected = records[i][1]
            records[i][1] = observed if expected in (None, observed) else "unstable"
            return value
        setattr(owner, name, call)
        undo.append((owner, name, original))
    def single_sql(args, kwargs):
        if len(args) == 1 and not kwargs:
            return args[0]

    def duckdb_sql(args, kwargs):
        if len(args) == 1 and kwargs == {"write": "duckdb"}:
            return args[0]

    def optimization_input(args, kwargs):
        if len(args) == 2 and kwargs == {"leave_tables_isolated": True, "dialect": None}:
            return args[0].dump()

    def generator_input(args, kwargs):
        if len(args) == 2 and not kwargs and args[0].identify is True and not args[0].comments:
            return args[1].dump()

    install(upstream, "parse_one", "parse", single_sql, lambda expr: expr.dump())
    install(upstream, "transpile", "transpile", duckdb_sql, lambda value: value)
    install(engine, "optimize", "optimize", optimization_input, lambda expr: expr.dump())
    install(PythonGenerator, "generate", "python", generator_input, lambda value: value)
    cls = upstream.TestExecutor
    cls.setUpClass()
    cases = cls.tpcds_sqls
    try:
        for case_number, case in enumerate(cases):
            cls.cache.clear()
            cls.tpcds_sqls = [case]
            result = unittest.TestResult()
            cls("test_execute_tpcds").run(result)
            if not result.wasSuccessful() or result.skipped:
                raise RuntimeError((result.errors, result.failures, result.skipped))
    finally:
        case_number = None
        cls.tearDownClass()
        cls.doClassCleanups()
    trace.active = 0
    for owner, name, orig in reversed(undo):
        setattr(owner, name, orig)
    # Verify every capture independently using exactly the submission's decoder.
    # If setup, object state or serialization cannot be reproduced, mark that
    # call as outside coverage so any intersecting edit retains the full test.
    invalid = 0
    for i, (record, expected) in enumerate(records):
        try:
            if None not in owners[i] and digest(evaluate(record)) == expected:
                continue
        except Exception:
            pass
        invalid |= 1 << (i + 1)
    masks = {}
    def intern(m):
        if m & invalid:
            m |= 1
        return masks.setdefault(m, len(masks))
    lines = {p: {n: intern(m) for n, m in rows.items()} for p, rows in trace.lines.items()}
    data = dict(owners=[sorted(n for n in queries if n is not None) for queries in owners],
                lines=lines, masks=[format(m, "x") for m in masks])
    for name, content in (("call-sites", data), ("call-inputs", records)):
        raw = json.dumps(content, separators=(",", ":")).encode()
        (directory / (name + ".json.gz")).write_bytes(gzip.compress(raw, mtime=0))
    print(f"{len(records)} captures, {invalid.bit_count()} retained conservatively", flush=True)


if __name__ == "__main__":
    main()
