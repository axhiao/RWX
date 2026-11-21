"""Verify changes to class-level fixtures against their actual consumers."""
import json
from pathlib import Path


def plans(base, paths, nodes, shared, optimizer):
    from tests.helpers import FIXTURES_DIR, load_sql_fixture_pairs

    pairs = {}
    for name in paths:
        # Use exactly the pinned loader. A broken header can fail setUpClass
        # before any method runs, even for methods that never use this list.
        before = list(load_sql_fixture_pairs(str(base / "tests/fixtures" / name)))
        after = list(load_sql_fixture_pairs(str(Path(FIXTURES_DIR) / name)))
        if len(before) != len(after):
            return [], {}
        pairs[name] = (len(after), [i for i, (a, b) in enumerate(zip(before, after)) if a != b])

    passed, cases = [], {}
    for node in nodes:
        method = node.rsplit("::", 1)[-1]
        reads, selections = [], {}
        for name, (size, changed) in pairs.items():
            consumers = shared[name]
            if node in consumers:
                if consumers[node] and changed:
                    reads.append((consumers[node], changed))
            elif (node.startswith("tests/test_optimizer.py::TestOptimizer::")
                  and name in optimizer.get(method, {})
                  and optimizer[method][name][1] == size):
                if changed:
                    selections[name] = [size, changed]
            else:
                # This node was included by some source of coverage for which
                # the fixture reader or access pattern is unknown.
                break
        else:
            if not reads and not selections:
                passed.append(node)
            elif len(reads) == 1 and not selections and len(reads[0][1]) <= 3:
                cases[node] = reads[0]
            elif selections and not reads and sum(len(a[1]) for a in selections.values()) <= 3:
                cases[node] = selections
    return passed, cases


if __name__ == "__main__":
    import sys
    from replay import _run_tests_in_process
    root, request, receipt = sys.argv[1:]
    sys.path.insert(0, root)
    base, paths, nodes, shared, optimizer = json.loads(Path(request).read_bytes())
    passed, cases = plans(Path(base), paths, nodes, shared, optimizer)
    passed.extend(_run_tests_in_process(list(cases), cases))
    Path(receipt).write_text(json.dumps(passed))
