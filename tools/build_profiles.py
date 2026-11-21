"""Collect baseline value reads and scalar operands, without applying any patches."""
import argparse
import ast
import builtins
from collections import defaultdict
import concurrent.futures
from functools import lru_cache
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from build_index import InlinePool, PIN
from operand_types import SQLGlotFinder


class ValueReads(dict):
    """Retain value origins across spreads, overrides and shallow copies.

    Membership, length and key iteration do not inspect any values. Iterators
    over values conservatively credit all entries to the current context.
    """
    def __init__(self, profile):
        super().__init__()
        self.origins = {}
        self.profile = profile

    def __getitem__(self, key):
        self.profile.read(self.origins.get(key))
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.profile.read(self.origins.get(key))
        return super().get(key, default)

    def __iter__(self):
        # Overriding this also makes uninstrumented dict(**table) copies
        # request values through __getitem__ rather than CPython's fast path.
        return super().__iter__()

    def all_values(self):
        for site in self.origins.values():
            self.profile.read(site)

    def items(self):
        self.all_values()
        return super().items()

    def values(self):
        self.all_values()
        return super().values()

    def copy(self):
        copied = ValueReads(self.profile)
        for k, v in dict.items(self):
            dict.__setitem__(copied, k, v)
        copied.origins.update(self.origins)
        return copied

    def __eq__(self, other):
        self.all_values()
        if isinstance(other, ValueReads):
            other.all_values()
        return super().__eq__(other)

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        self.all_values()
        return super().__repr__()

    def __or__(self, other):
        self.all_values()
        if isinstance(other, ValueReads):
            other.all_values()
        return super().__or__(other)

    def __ror__(self, other):
        self.all_values()
        return super().__ror__(other)

    def setdefault(self, key, default=None):
        self.profile.read(self.origins.get(key))
        return super().setdefault(key, default)

    def pop(self, key, *default):
        self.profile.read(self.origins.get(key))
        return super().pop(key, *default)

    def popitem(self):
        key, value = super().popitem()
        self.profile.read(self.origins.get(key))
        return key, value


CMP = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def configuration_fields(root):
    fields, reads, reflective = set(), set(), set()
    for folder in ("sqlglot", "tests"):
        for file in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(file.read_bytes())):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    reads.add(node.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value.isidentifier():
                        reflective.add(node.value)
                elif isinstance(node, ast.ClassDef) and folder == "sqlglot":
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                            target = stmt.targets[0]
                        elif isinstance(stmt, ast.AnnAssign):
                            target = stmt.target
                        else:
                            continue
                        if isinstance(target, ast.Name) and target.id.isupper():
                            fields.add(target.id)
                    # A second class field can derive its value from a bare
                    # class-local name while Python is constructing the class.
                    for stmt in node.body:
                        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value:
                            reflective.update(n.id for n in ast.walk(stmt.value)
                                              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load))
    return (fields & reads) - reflective


class ProfileProbe(ast.NodeTransformer):
    fields = set()

    def __init__(self, path):
        self.path = path

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        for stmt in node.body:
            if not isinstance(stmt, (ast.AnnAssign, ast.Assign)):
                continue
            value = stmt.value
            if not isinstance(value, ast.Dict) or not value.keys:
                continue
            entries = []
            for key, val in zip(value.keys, value.values):
                if key is None:
                    entry = [val]
                else:
                    site = f"{self.path}:{key.lineno}:{key.col_offset}"
                    entry = [ast.Constant(site), key, val]
                entries.append(ast.Tuple(entry, ast.Load()))
            call = ast.Call(ast.Name("_blast_table", ast.Load()), [ast.List(entries, ast.Load())], [])
            stmt.value = ast.copy_location(call, value)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.fields:
            call = ast.Call(ast.Name("_blast_field", ast.Load()), [node.value, ast.Constant(node.attr)], [])
            return ast.copy_location(call, node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) in (2, 3):
            node.func = ast.copy_location(ast.Name("_blast_getattr", ast.Load()), node.func)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and isinstance(node.ops[0], CMP):
            rhs = node.comparators[0]
            if isinstance(rhs, ast.Constant) or (isinstance(rhs, ast.Tuple) and
                                                 all(isinstance(x, ast.Constant) for x in rhs.elts)):
                site = f"{self.path}:{node.lineno}:{node.col_offset}"
                call = ast.Call(ast.Name("_blast_scalar", ast.Load()), [ast.Constant(site), node.left], [])
                node.left = ast.copy_location(call, node.left)
        return node


def safe_scalar(value):
    t = type(value)
    if t in (type(None), bool):
        return True
    if t is int:
        return value.bit_length() <= 63
    if t is str:
        return len(value) <= 40
    if t is tuple:
        return len(value) <= 5 and all(type(x) in (int, bool, str) and safe_scalar(x) for x in value)
    return False


class Profile:
    def __init__(self):
        self.entries = {}
        self.observed = set()
        self.opaque = set()
        self.scalars = defaultdict(dict)
        self.samples = {}
        self.active = False
        self.field_reads = {}
        self.field_seen = set()
        self.cold_fields = set()

    @staticmethod
    @lru_cache(maxsize=65536)
    def declaring_class(cls, name):
        for base in cls.__mro__:
            if name in vars(base):
                return base.__module__ + "." + base.__qualname__ + "." + name
        return None

    def lookup(self, receiver, name, *default):
        if isinstance(name, str) and name in ProfileProbe.fields:
            self.observe_field(receiver, name)
        return getattr(receiver, name, *default)

    def field(self, receiver, name):
        self.observe_field(receiver, name)
        return getattr(receiver, name)

    def observe_field(self, receiver, name):
        cls = receiver if isinstance(receiver, type) else type(receiver)
        site = self.declaring_class(cls, name)
        if site:
            if self.active:
                self.field_seen.add(site)
            else:
                self.cold_fields.add(site)

    def read(self, site):
        if site is not None:
            if self.active:
                self.observed.add(site)
            else:
                self.opaque.add(site)

    def table(self, rows):
        table = ValueReads(self)
        for row in rows:
            if len(row) == 1:
                source = row[0]
                if isinstance(source, ValueReads):
                    for key, value in dict.items(source):
                        table[key] = value
                        table.origins[key] = source.origins.get(key)
                else:
                    for key in source:
                        table[key] = source[key]
                        table.origins.pop(key, None)
            else:
                site, key, value = row
                self.entries.setdefault(site, 0)
                table[key] = value
                table.origins[key] = site
        return table

    def scalar(self, site, value):
        if self.active:
            values = self.samples.setdefault(site, set())
            if "?" not in values:
                if not safe_scalar(value) or len(values) >= 32:
                    values.add("?")
                else:
                    values.add(repr(value))
        return value

    def begin(self):
        self.active = True

    def finish(self, mask):
        self.active = False
        for field in self.field_seen:
            self.field_reads[field] = self.field_reads.get(field, 0) | mask
        self.field_seen.clear()
        for site in self.observed:
            self.entries[site] |= mask
        for site, values in self.samples.items():
            if "?" in values:
                values = ("?",)
            for value in values:
                table = self.scalars[site]
                table[value] = table.get(value, 0) | mask
        self.observed.clear()
        self.samples.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "catalog", "output"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    names = sorted(t["id"] for t in json.loads(args.catalog.read_bytes())["tests"])
    if (subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"], text=True).strip() != PIN
            or subprocess.run(["git", "-C", root, "diff", "--quiet", "HEAD"]).returncode):
        parser.error("expected the pristine pinned SQLGlot revision")
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, str(root))
    profile = Profile()
    ProfileProbe.fields = configuration_fields(root)
    builtins._blast_getattr = profile.lookup
    builtins._blast_field = profile.field
    builtins._blast_table = profile.table
    builtins._blast_scalar = profile.scalar
    sys.meta_path.insert(0, SQLGlotFinder(ProfileProbe, ("sqlglot", "tests")))
    concurrent.futures.ProcessPoolExecutor = InlinePool
    from sqlglot.dialects import Dialect, Dialects
    # Resolve lazy startup caches before attaching observations to one test.
    for name in Dialects:
        d = Dialect.get_or_raise(name)
        d.tokenizer()
        d.parser()
        d.generator()
    groups = defaultdict(list)
    for idx, name in enumerate(names):
        file, cls, method = name.split("::")
        mod = importlib.import_module(file[:-3].replace("/", "."))
        groups[getattr(mod, cls)].append((idx, method))
    complete = 0
    for cls, methods in groups.items():
        mask = sum(1 << i for i, _ in methods)
        profile.begin()
        try:
            cls.setUpClass()
        finally:
            profile.finish(mask)
        for idx, method in methods:
            profile.begin()
            try:
                result = unittest.TestResult()
                cls(method).run(result)
            finally:
                profile.finish(1 << idx)
            if result.wasSuccessful() and not result.skipped:
                complete |= 1 << idx
            else:
                print("incomplete", names[idx], (result.failures + result.errors + result.skipped)[0][1][-360:], file=sys.stderr)
        profile.begin()
        try:
            cls.tearDownClass()
            cls.doClassCleanups()
        finally:
            profile.finish(mask)
        print(cls.__name__, file=sys.stderr, flush=True)
    masks = {}
    def intern(mask):
        return masks.setdefault(mask, len(masks))
    entries = {s: intern(m) for s, m in sorted(profile.entries.items()) if s not in profile.opaque}
    scalars = {s: {v: intern(m) for v, m in sorted(rows.items())} for s, rows in sorted(profile.scalars.items())}
    fields = {site: intern(m) for site, m in sorted(profile.field_reads.items()) if site not in profile.cold_fields}
    data = dict(fields=fields, commit=PIN, tests=names, complete=intern(complete), entries=entries,
                scalars=scalars, masks=[format(n, "x") for n in masks])
    temp = output.with_suffix(".tmp")
    temp.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    temp.replace(output)
    print(f"{complete.bit_count()}/{len(names)} tests; {len(entries)} value sites and {len(scalars)} comparisons", file=sys.stderr)


if __name__ == "__main__":
    main()
