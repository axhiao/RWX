"""Record baseline inputs, outcomes and individual call coverage for the node-limit test."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from build_index import PIN, Recorder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected the pristine pinned checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    record = Recorder(root, None)
    record.active = 1
    from tests import test_parser
    from sqlglot.errors import ParseError
    original = test_parser.parse_one
    samples = []

    def capture(sql, **opts):
        row = dict(sql=hashlib.sha256(sql.encode()).hexdigest(), opts=opts)
        samples.append(row)
        record.active = 1 << len(samples)
        sys.monitoring.restart_events()
        start = time.process_time()
        try:
            expression = original(sql, **opts)
        except ParseError as exc:
            record.active = 0
            row.update(error=str(exc), details=exc.errors)
            raise
        else:
            record.active = 0
            row["tree"] = expression.dump()
            return expression
        finally:
            row["cost"] = round(1000 * (time.process_time() - start))
            record.active = 1
            sys.monitoring.restart_events()

    test_parser.parse_one = capture
    result = unittest.TestResult()
    test_parser.TestParser("test_max_nodes").run(result)
    if not result.wasSuccessful() or result.skipped:
        raise RuntimeError((result.errors, result.failures, result.skipped))
    masks = {}
    lines = {p: {n: masks.setdefault(m, len(masks)) for n, m in rows.items()}
             for p, rows in record.lines.items() if p.startswith("sqlglot/")}
    data = dict(cases=samples, lines=lines, masks=[format(m, "x") for m in masks])
    output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))


if __name__ == "__main__":
    main()
