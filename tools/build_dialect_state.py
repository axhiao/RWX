"""Measure actual reads of derived dialect state on the unmodified SQLGlot suite."""
import argparse
from collections import defaultdict
import concurrent.futures
import gzip
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from build_index import InlinePool, PIN

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dialect_state import fields, state


class Map(dict):
    def __init__(self, table, observe):
        super().__init__(table)
        self.observe = observe

    def get(self, k, *default):
        self.observe("get" + repr(k))
        return super().get(k, *default)

    def __getitem__(self, k):
        self.observe("get" + repr(k))
        return super().__getitem__(k)

    def __contains__(self, k):
        self.observe("in" + repr(k))
        return super().__contains__(k)

    def __iter__(self):
        self.observe("keys")
        return super().__iter__()

    def keys(self):
        self.observe("keys")
        return super().keys()

    def __bool__(self):
        self.observe("bool")
        return dict.__len__(self) != 0

    def __len__(self):
        self.observe("len")
        return super().__len__()


class Members(set):
    def __init__(self, table, observe):
        super().__init__(table)
        self.observe = observe

    def __contains__(self, k):
        self.observe("in" + repr(k))
        return super().__contains__(k)

    def __iter__(self):
        self.observe("keys")
        return super().__iter__()

    def __bool__(self):
        self.observe("bool")
        return set.__len__(self) != 0

    def __len__(self):
        self.observe("len")
        return super().__len__()


def whole(method):
    def wrapped(self, *args, **kwargs):
        self.observe("all")
        for arg in args:
            if isinstance(arg, (Members, Map)):
                arg.observe("all")
        return method(self, *args, **kwargs)
    return wrapped


for cls, base, methods in [
    (Map, dict, ("copy", "items", "values", "setdefault", "pop", "popitem", "__or__", "__ror__",
                 "__eq__", "__ne__", "__repr__", "__str__", "__reversed__")),
    (Members, set, ("copy", "union", "intersection", "difference", "symmetric_difference", "issubset",
                    "issuperset", "isdisjoint", "__and__", "__or__", "__xor__", "__sub__", "__eq__",
                    "__ne__", "__le__", "__ge__", "__lt__", "__gt__", "__repr__", "__str__")),
]:
    for name in methods:
        setattr(cls, name, whole(getattr(base, name)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "catalog", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    names = sorted(t["id"] for t in json.loads(args.catalog.read_bytes())["tests"])
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected the pristine pinned revision")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    base = state()
    sites = defaultdict(list)
    rows = list(fields())
    for _, _, table, site in rows:
        sites[id(table)].append(site)
    active = (1 << len(names)) - 1
    reads = defaultdict(dict)
    wrappers = {}
    def observer(key):
        def observe(event):
            table = reads[key]
            previous = table.get(event, 0)
            if previous & active != active:
                table[event] = previous | active
        return observe
    for cls, name, table, _ in rows:
        key = id(table)
        if key not in wrappers:
            wrappers[key] = (Map if isinstance(table, dict) else Members)(table, observer(key))
        setattr(cls, name, wrappers[key])

    groups = defaultdict(list)
    for i, node in enumerate(names):
        file, class_name, method = node.split("::")
        mod = importlib.import_module(file[:-3].replace("/", "."))
        groups[getattr(mod, class_name)].append((i, method))
    complete = 0
    for cls, methods in groups.items():
        class_mask = sum(1 << i for i, _ in methods)
        active = class_mask
        cls.setUpClass()
        for i, method in methods:
            active = 1 << i
            result = unittest.TestResult()
            cls(method).run(result)
            if result.wasSuccessful() and not (result.skipped or result.expectedFailures):
                complete |= active
            else:
                print("incomplete", names[i], file=sys.stderr)
        active = class_mask
        cls.tearDownClass()
        cls.doClassCleanups()
    masks = {}
    def intern(mask):
        return masks.setdefault(mask, len(masks))
    packed = {site: {e: intern(m) for e, m in sorted(events.items())}
              for key, events in reads.items() for site in sites[key]}
    data = dict(tests=names, base=base, reads=packed, complete=intern(complete),
                masks=[format(n, "x") for n in masks])
    output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{complete.bit_count()}/{len(names)} measured, {output.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
