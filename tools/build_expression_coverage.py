"""Trace evaluation before individual short-circuit operands, comparisons and calls."""
import argparse
import ast
import builtins
from collections import defaultdict
import concurrent.futures
from contextlib import redirect_stdout
import gzip
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from build_index import InlinePool, PIN
from operand_types import SQLGlotFinder
from expression_coverage import location


class Probe(ast.NodeTransformer):
    sites = {}
    files = defaultdict(dict)

    def __init__(self, path):
        self.path = path
        self.in_body = False

    def point(self, node):
        key = location(node)
        sites = self.files[self.path]
        if key not in sites:
            sites[key] = len(self.sites)
            self.sites[self.path, key] = sites[key]
        mark = ast.Call(ast.Name("_rwx_enter_expression", ast.Load()), [ast.Constant(sites[key])], [])
        return ast.copy_location(ast.BoolOp(ast.Or(), [mark, node]), node)

    def visit_FunctionDef(self, node):
        previous, self.in_body = self.in_body, True
        node.body = [self.visit(stmt) for stmt in node.body]
        self.in_body = previous
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        previous, self.in_body = self.in_body, True
        node.body = self.visit(node.body)
        self.in_body = previous
        return node

    def visit_ClassDef(self, node):
        previous, self.in_body = self.in_body, False
        node.body = [self.visit(stmt) for stmt in node.body]
        self.in_body = previous
        return node

    def visit(self, node):
        # Remember the original node, so instrumentation never instruments itself.
        wrapped = self.in_body and isinstance(node, (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.IfExp))
        if self.in_body and isinstance(node, ast.Call):
            wrapped = any(isinstance(arg, ast.Constant) for arg in (*node.args, *(kw.value for kw in node.keywords)))
        node = super().visit(node)
        if wrapped:
            return self.point(node)
        return node


def trace_import(root, module):
    root = Path(root).resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    seen = set()
    def enter(site):
        seen.add(site)
    builtins._rwx_enter_expression = enter
    sys.meta_path.insert(0, SQLGlotFinder(Probe))
    with redirect_stdout(sys.stderr):
        importlib.import_module(module)
    print(json.dumps({path: [key for key, site in table.items() if site in seen]
                      for path, table in Probe.files.items()}))


def include_imports(root, data):
    groups = defaultdict(int)
    for i, name in enumerate(data["tests"]):
        groups[name.split("::", 1)[0]] |= 1 << i
    def collect(file):
        module = file[:-3].replace("/", ".")
        run = subprocess.run([sys.executable, __file__, "--import-only", str(root), module],
                             capture_output=True, check=True)
        return file, json.loads(run.stdout)
    masks = [int(value, 16) for value in data["masks"]]
    ids = {mask: i for i, mask in enumerate(masks)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
        for file, sites in workers.map(collect, groups):
            group = groups[file]
            for path, keys in sites.items():
                table = data["files"].get(path, {})
                for key in keys:
                    if key in table:
                        value = masks[table[key]] | group
                        if value not in ids:
                            ids[value] = len(masks)
                            masks.append(value)
                        table[key] = ids[value]
    data["masks"] = [format(mask, "x") for mask in masks]
    data["includes_imports"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "catalog", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    names = sorted(t["id"] for t in json.loads(args.catalog.read_bytes())["tests"])
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected a clean pinned checkout")
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    active = 0
    uncertain = 0
    cold, seen = set(), set()
    hits = defaultdict(int)

    def enter(site):
        if active:
            seen.add(site)
        else:
            cold.add(site)

    def audit(event, args):
        nonlocal uncertain
        if event == "subprocess.Popen":
            uncertain |= active

    def finish():
        for site in seen:
            hits[site] |= active
        seen.clear()

    builtins._rwx_enter_expression = enter
    sys.addaudithook(audit)
    sys.meta_path.insert(0, SQLGlotFinder(Probe))
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from sqlglot.dialects import Dialect, Dialects
    for dialect in Dialects:
        instance = Dialect.get_or_raise(dialect)
        instance.parser()
        instance.generator()
        instance.tokenizer()
    groups = defaultdict(list)
    for i, name in enumerate(names):
        file, cls, method = name.split("::")
        module = importlib.import_module(file[:-3].replace("/", "."))
        groups[getattr(module, cls)].append((i, method))

    for cls, methods in groups.items():
        class_mask = sum(1 << i for i, _ in methods)
        active = class_mask
        cls.setUpClass()
        finish()
        for i, method in methods:
            active = 1 << i
            result = unittest.TestResult()
            cls(method).run(result)
            if not result.wasSuccessful() or result.skipped or result.expectedFailures:
                uncertain |= active
            finish()
        active = class_mask
        cls.tearDownClass()
        cls.doClassCleanups()
        finish()
        active = 0
        print(cls.__name__, file=sys.stderr, flush=True)
    masks = {}
    def intern(value):
        return masks.setdefault(value, len(masks))
    files = {p: {key: intern(hits[i]) for key, i in sites.items() if i not in cold}
             for p, sites in Probe.files.items()}
    data = dict(files=files, tests=names, uncertain=intern(uncertain), masks=[format(m, "x") for m in masks])
    include_imports(root, data)
    output.write_bytes(gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(Probe.sites)} expressions, {len(cold)} startup sites, {uncertain.bit_count()} uncertain tests")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--import-only"]:
        trace_import(*sys.argv[2:])
    else:
        main()
