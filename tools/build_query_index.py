"""Measure executable lines for each independent TPC fixture on the pinned revision."""
import argparse
import concurrent.futures
import gzip
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from build_index import InlinePool, PIN, Recorder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if (subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", source, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected a clean pinned checkout")
    os.chdir(source)
    sys.path.insert(0, str(source))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    recorder = Recorder(source, None)
    def activate(bit):
        recorder.active = bit
        sys.monitoring.restart_events()
    activate(1)
    cls = importlib.import_module("tests.test_executor").TestExecutor
    records = {}
    next_bit = 1
    for method, field in (("test_execute_tpcds", "tpcds_sqls"), ("test_execute_tpch", "tpch_sqls"),
                          ("test_optimized_tpch", "tpch_sqls")):
        activate(1)
        cls.setUpClass()
        cases = getattr(cls, field)
        start_bit = next_bit
        costs = []
        try:
            for case in cases:
                setattr(cls, field, [case])
                cls.cache.clear()
                activate(1 << next_bit)
                start = time.process_time()
                result = unittest.TestResult()
                cls(method).run(result)
                costs.append(max(1, round((time.process_time() - start) * 1000)))
                if not result.wasSuccessful() or result.skipped:
                    raise RuntimeError((method, next_bit, result.failures, result.errors))
                next_bit += 1
        finally:
            activate(1)
            cls.tearDownClass()
            cls.doClassCleanups()
        records[method] = [field, start_bit, costs]
        print(method, len(cases), sum(costs), flush=True)
    masks = {}
    lines = {p: {n: masks.setdefault(v, len(masks)) for n, v in rows.items()}
             for p, rows in recorder.lines.items() if p.startswith("sqlglot/")}
    data = dict(methods=records, lines=lines, masks=[format(m, "x") for m in masks])
    output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    main()
