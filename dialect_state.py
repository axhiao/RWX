"""Observe the initialized tables produced by SQLGlot's dialect metaclass."""
import ast
import gzip
import json
from pathlib import Path

# These inputs to _Dialect.__new__ produce string maps, tries and sets.
# Trace the materialized fields, including inherited copies and inverse maps.
INPUTS = {"DATE_PART_MAPPING", "UNESCAPED_SEQUENCES", "TIME_MAPPING", "FORMAT_MAPPING",
          "INVERSE_TIME_MAPPING", "CREATABLE_KIND_MAPPING"}
FIELDS = INPUTS | {"VALID_INTERVAL_UNITS", "ESCAPED_SEQUENCES", "IDENTIFIER_ESCAPED_SEQUENCES",
                   "TIME_TRIE", "FORMAT_TRIE", "INVERSE_TIME_TRIE", "INVERSE_FORMAT_TRIE",
                   "INVERSE_FORMAT_MAPPING", "INVERSE_CREATABLE_KIND_MAPPING"}


def fields():
    from sqlglot.dialects import Dialect, Dialects
    from sqlglot.executor.python import Python

    classes = {Dialect, Python}
    for name in Dialects:
        classes.update(c for c in type(Dialect.get_or_raise(name)).__mro__ if issubclass(c, Dialect))
    objects = {id(value) for cls in classes for name, value in vars(cls).items()
               if name in FIELDS and type(value) in (dict, set)}
    for cls in sorted(classes, key=lambda c: (c.__module__, c.__qualname__)):
        for name, value in vars(cls).items():
            if id(value) in objects:
                yield cls, name, value, f"{cls.__module__}.{cls.__qualname__}.{name}"


def state():
    # Encode the value below each top-level key. Trie walks read their first
    # character at the root, so a changed subtree invalidates that character.
    return {site: ({repr(k): repr(v) for k, v in table.items()} if isinstance(table, dict)
                   else {repr(k): "" for k in sorted(table)})
            for _, _, table, site in fields()}


def eligible(src, changed, lines):
    """Prove that only existing string values in top-level dialect classes changed."""
    def assignments(tree):
        return {(cls.name, stmt.targets[0].id): stmt.value for cls in tree.body if isinstance(cls, ast.ClassDef)
                for stmt in cls.body if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id in INPUTS
                and isinstance(stmt.value, ast.Dict)}
    originals, replacements = assignments(src.tree), assignments(changed)
    undo = []
    try:
        for name, orig in originals.items():
            new = replacements.get(name)
            if not new or len(orig.values) != len(new.values):
                continue
            for i, (a, b) in enumerate(zip(orig.values, new.values)):
                if (isinstance(a, ast.Constant) and type(a.value) is str and a.lineno in lines
                        and isinstance(b, ast.Constant) and type(b.value) is str and a.value != b.value):
                    undo.append((new, i, b))
                    new.values[i] = a
        return bool(undo) and ast.dump(changed) == ast.dump(src.tree)
    finally:
        for node, i, value in undo:
            node.values[i] = value


class StateCoverage:
    def __init__(self, path, tests):
        with gzip.open(path, "rt") as stream:
            data = json.load(stream)
        if data["tests"] != tests:
            raise ValueError("dialect state index uses different test IDs")
        self.base = data["base"]
        self.reads = data["reads"]
        self.masks = [int(m, 16) for m in data["masks"]]
        self.complete = self.masks[data["complete"]]

    def impact(self, other):
        if self.base.keys() != other.keys():
            return None
        mask = 0
        for site, before in self.base.items():
            after = other[site]
            if before == after and list(before) == list(after):
                continue
            events = {"all"}
            if len(before) != len(after):
                events.add("len")
            if bool(before) != bool(after):
                events.add("bool")
            if list(before) != list(after):
                events.add("keys")
            for key in before.keys() | after.keys():
                if (key in before) != (key in after):
                    events.add("in" + key)
                if before.get(key) != after.get(key):
                    events.add("get" + key)
            reads = self.reads.get(site, {})
            for event in events:
                if event in reads:
                    mask |= self.masks[reads[event]]
        return mask


if __name__ == "__main__":
    import sys
    sys.path.insert(0, sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(state(), separators=(",", ":")))
