"""Trace independent SQL fixture pairs, including optimizer worker serialization and assertions."""
import argparse
from collections import defaultdict
import concurrent.futures
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from build_index import InlinePool, PIN, Recorder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "catalog", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    methods = [t["id"].rsplit("::", 1)[1] for t in json.loads(args.catalog.read_bytes())["tests"]
               if t["id"].startswith("tests/test_optimizer.py::TestOptimizer::")]
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected a clean pinned checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from tests import test_optimizer as upstream
    cls = upstream.TestOptimizer
    recorder = Recorder(root, None)
    ranges = defaultdict(dict)
    check_file, load = cls.check_file, upstream.load_sql_fixture_pairs
    next_bit = 0
    def select(bit):
        recorder.active = bit
        sys.monitoring.restart_events()
    def reserve(name, size):
        nonlocal next_bit
        if name not in ranges[method]:
            ranges[method][name] = [next_bit, size]
            next_bit += size
        first, count = ranges[method][name]
        assert count == size
        return first
    def one_at_a_time(self, file, *args, **kwargs):
        name = f"optimizer/{file}.sql"
        pairs = list(load(name))
        first = reserve(name, len(pairs))
        try:
            for i, pair in enumerate(pairs):
                upstream.load_sql_fixture_pairs = lambda filename: [pair] if filename == name else load(filename)
                select(1 << (first + i))
                check_file(self, file, *args, **kwargs)
        finally:
            select(0)
            upstream.load_sql_fixture_pairs = tracked_load
    def tracked_load(name):
        if method != "test_annotate_funcs":
            yield from load(name)
            return
        pairs = list(load(name))
        first = reserve(name, len(pairs))
        try:
            for i, pair in enumerate(pairs):
                select(1 << (first + i))
                yield pair
        finally:
            select(0)
    cls.check_file = one_at_a_time
    upstream.load_sql_fixture_pairs = tracked_load
    cls.setUpClass()
    for method in methods:
        result = unittest.TestResult()
        cls(method).run(result)
        if not result.wasSuccessful() or result.skipped:
            ranges.pop(method, None)
            print("kept full method", method, file=sys.stderr)
    cls.tearDownClass()
    cls.doClassCleanups()
    masks = {}
    lines = {p: {n: masks.setdefault(m, len(masks)) for n, m in rows.items()}
             for p, rows in recorder.lines.items() if p.startswith("sqlglot/")}
    data = dict(ranges=ranges, lines=lines, masks=[format(m, "x") for m in masks])
    output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{next_bit} fixture entries in {len(ranges)} test methods")


if __name__ == "__main__":
    main()
