"""Choose SQLGlot tests from the dependencies measured on the pinned revision."""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
import heapq
import json
from pathlib import Path
import re
import time
from replay import verify_commit, verify_fixtures, patched_state, warmup
from checkpoints import Checkpoints, TPCDS
from dialect_state import StateCoverage, eligible
from query_replay import QueryCoverage

from value_profiles import Profiles
from expression_coverage import ExpressionCoverage


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
DIALECT_DIRS = {"dialects", "generators", "parsers", "typing"}


@dataclass
class FileEdit:
    old: str
    new: str
    # A hunk is (zero-based source offset, original context, replacement).
    hunks: list = field(default_factory=list)
    touched: set = field(default_factory=set)
    insertions: set = field(default_factory=set)
    new_lines: set = field(default_factory=set)

    final_newline: bool | None = None

    def apply(self, source):
        lines = source.removesuffix("\n").split("\n") if source else []
        result, cursor = [], 0
        for start, before, after in self.hunks:
            if start < cursor or lines[start:start + len(before)] != before:
                raise ValueError("hunk context does not match the pinned tree")
            result.extend(lines[cursor:start])
            result.extend(after)
            cursor = start + len(before)
        ending = self.final_newline if self.final_newline is not None else source.endswith("\n")
        return "\n".join(result + lines[cursor:]) + ("\n" if ending else "")


def diff_path(header):
    path = header[4:].split("\t", 1)[0]
    if path == "/dev/null":
        return ""
    if not path.startswith(("a/", "b/")) or ".." in Path(path).parts or path.startswith(("a//", "b//")):
        raise ValueError("unsupported path")
    return path[2:]


def parse_patch(text):
    lines = text.split("\n")
    edits = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            if lines[i].startswith(("Binary files ", "GIT binary patch", "rename from ", "copy from ", "old mode ", "new mode ")):
                raise ValueError("non-text change")
            i += 1
            continue
        old = diff_path(lines[i])
        i += 1
        if i == len(lines) or not lines[i].startswith("+++ "):
            raise ValueError("missing target header")
        edit = FileEdit(old, diff_path(lines[i]))
        edits.append(edit)
        i += 1
        while i < len(lines) and (match := HUNK.match(lines[i])):
            start, old_count, new_start, new_count = match.groups()
            old_count = int(old_count or 1)
            new_count = int(new_count or 1)
            offset = int(start) - int(old_count > 0)
            before, after = [], []
            deleted_in_group = False
            inserted_at = None
            i += 1
            previous = None
            while len(before) < old_count or len(after) < new_count:
                if i >= len(lines) or not lines[i]:
                    raise ValueError("truncated hunk")
                op, body = lines[i][0], lines[i][1:]
                i += 1
                if op == "\\":
                    edit.final_newline = previous == "-"
                    continue
                previous = op
                if op not in " +-":
                    raise ValueError("unexpected hunk line")
                if op == " ":
                    if inserted_at is not None and not deleted_in_group:
                        edit.insertions.add(inserted_at)
                    inserted_at, deleted_in_group = None, False
                if op == "-":
                    edit.touched.add(offset + len(before) + 1)
                    deleted_in_group = True
                if op == "+":
                    edit.new_lines.add(int(new_start) + len(after))
                    if inserted_at is None:
                        inserted_at = offset + len(before)
                if op != "+":
                    before.append(body)
                if op != "-":
                    after.append(body)
            if inserted_at is not None and not deleted_in_group:
                edit.insertions.add(inserted_at)
            edit.hunks.append((offset, before, after))
            if i < len(lines) and lines[i].startswith("\\ No newline"):
                edit.final_newline = previous == "-"
                i += 1
        if not edit.hunks:
            raise ValueError("no text hunks")
    if not edits:
        raise ValueError("no file headers")
    return edits


def union(masks):
    result = 0
    for mask in masks:
        result |= mask
    return result


class Source:
    def __init__(self, path):
        self.text = path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.text)
        self.parents = {child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)}
        self.nodes = [n for n in ast.walk(self.tree) if isinstance(n, (ast.stmt, ast.Lambda))]
        self.conditions = [n for n in ast.walk(self.tree) if isinstance(n, (ast.BoolOp, ast.Compare, ast.IfExp))
                           and n.lineno < n.end_lineno]

    def scope(self, line):
        candidates = [n for n in self.nodes if n.lineno <= line <= n.end_lineno]
        if not candidates:
            return []
        current = min(candidates, key=lambda n: (n.end_lineno - n.lineno, -n.col_offset))
        chain = [current]
        while current in self.parents:
            current = self.parents[current]
            chain.append(current)
        return chain


class Impact:
    def __init__(self, source, index, tests):
        self.source = source
        data = json.loads(index.read_bytes())
        masks = [int(v, 16) for v in data["masks"]]
        self.lines = {p: {int(n): masks[v] for n, v in rows.items()} for p, rows in data["lines"].items()}
        self.imports = {p: {int(n): masks[v] for n, v in rows.items()}
                        for p, rows in data.get("imports", {}).items()}
        for path, rows in self.imports.items():
            combined = self.lines.setdefault(path, {})
            for line, mask in rows.items():
                combined[line] = combined.get(line, 0) | mask
        self.files, self.dialects, self.expressions = (
            {key: masks[v] for key, v in data[table].items()} for table in ("files", "dialects", "expressions")
        )
        self.operands = {site: {key: masks[v] for key, v in table.items()}
                         for site, table in data.get("operands", {}).items()}
        self.attributes = {k: masks[v] for k, v in data.get("attributes", {}).items()}
        self.by_file = defaultdict(int)
        self.numbers = {}
        ids = {name: i for i, name in enumerate(data["tests"])}
        self.all = 0
        for row in tests:
            name = row["id"]
            if name not in ids:
                ids[name] = len(ids)
            idx = ids[name]
            self.numbers[idx] = row["number"]
            self.by_file[row["file"]] |= 1 << idx
            self.all |= 1 << idx
        self.unmeasured = self.all & ~masks[data["complete"]]
        self.test_ids = data["tests"]
        expression_map = index.with_name("expressions.json.gz")
        self.precise = ExpressionCoverage(expression_map, self.test_ids) if expression_map.is_file() else None
        self.state = None
        profile_path = index.with_name("profiles.json")
        self.profiles = Profiles(profile_path, data["tests"], source) if profile_path.is_file() else None

    @lru_cache(maxsize=64)
    def model(self, path):
        return Source(self.source / path)

    def family(self, path):
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_DIRS:
            return self.dialects.get(Path(path).stem)
        return None

    def declaration(self, path, chain):
        owner = next((n for n in chain if isinstance(n, ast.ClassDef)), None)
        if owner and path.startswith("sqlglot/expressions/"):
            if owner.name in self.expressions:
                return self.expressions[owner.name]
        family = self.family(path)
        if family is not None:
            # A simple literal configuration can only propagate to tests
            # exercising its read sites. Import-time consumers are excluded
            # from this refinement when the map is built.
            statement = chain[0] if chain else None
            if owner and isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                literal = statement.value
                if isinstance(literal, ast.UnaryOp) and isinstance(literal.op, (ast.UAdd, ast.USub)):
                    literal = literal.operand
                if len(targets) == 1 and isinstance(targets[0], ast.Name) and isinstance(literal, ast.Constant):
                    readers = self.attributes.get(targets[0].id)
                    if readers is not None:
                        return family & readers
            return family
        if path in self.by_file:
            return self.by_file[path] | union(self.lines.get(path, {}).values())
        # Module/class initializers can have import-wide effects.
        return self.all

    def line_impact(self, path, src, line, insertion=False, rows=None):
        chain = src.scope(line)
        func = next((n for n in chain if isinstance(n, FUNCTION)), None)
        if not func or line == func.lineno and not isinstance(func, ast.Lambda):
            return self.declaration(path, chain)
        if rows is None:
            rows = self.lines.get(path, {})
        if insertion:
            return union(m for n, m in rows.items() if func.lineno <= n <= func.end_lineno)
        result = rows.get(line, 0)
        stmt = next((n for n in chain if isinstance(n, ast.stmt)), None)
        if stmt:
            result |= rows.get(stmt.lineno, 0)
            # An operator later in a multiline condition can make operands
            # reachable that the baseline short-circuited. The first operand
            # is an entry point even if the "if (" header has no bytecode.
            if isinstance(stmt, (ast.If, ast.While)) and line <= stmt.test.end_lineno:
                result |= rows.get(stmt.test.lineno, 0)
        for expr in src.conditions:
            if expr.lineno <= line <= expr.end_lineno:
                result |= rows.get(expr.lineno, 0)
        return result

    def identity_change(self, path, src, changed):
        """Use observed operand types only when identity/equality is the entire edit."""
        old_comparisons = [n for n in ast.walk(src.tree) if isinstance(n, ast.Compare)]
        new_comparisons = [n for n in ast.walk(changed) if isinstance(n, ast.Compare)]
        if len(old_comparisons) != len(new_comparisons):
            return None
        affected = 0
        found = False
        for old, new in zip(old_comparisons, new_comparisons):
            if len(old.ops) != 1 or len(new.ops) != 1:
                continue
            operators = type(old.ops[0]), type(new.ops[0])
            if operators not in ((ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)):
                continue
            observations = self.operands.get(f"{path}:{old.lineno}:{old.col_offset}")
            if not observations:
                return None
            affected |= self.imports.get(path, {}).get(old.lineno, 0)
            for pair, mask in observations.items():
                left, right = pair.split("|")
                identity_equality = left == right == "identity"
                same_enum = left == right and left.startswith("enum:")
                none_comparison = "none" in (left, right) and "other" not in (left, right)
                if not (identity_equality or same_enum or none_comparison):
                    affected |= mask
            new.ops[0] = old.ops[0]
            found = True
        if found and ast.dump(src.tree) == ast.dump(changed):
            return affected
        return None

    @staticmethod
    def import_operations(tree, changed_lines):
        """Find eager operations in the module or class body at changed lines."""
        operations = set()
        pending = list(tree.body)
        while pending:
            node = pending.pop()
            first_line = min((d.lineno for d in getattr(node, "decorator_list", ())), default=node.lineno)
            if not any(first_line <= line <= node.end_lineno for line in changed_lines):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.lineno in changed_lines or any(
                    d.lineno <= line <= d.end_lineno for d in node.decorator_list for line in changed_lines
                ):
                    operations.add(("definition", node.name, ast.dump(node.args),
                                    tuple(ast.dump(d) for d in node.decorator_list)))
                continue
            if isinstance(node, ast.ClassDef):
                if node.lineno in changed_lines or first_line < node.lineno and first_line in changed_lines:
                    operations.add(("class", node.name, tuple(ast.dump(b) for b in node.bases),
                                    tuple(ast.dump(d) for d in node.decorator_list)))
                pending.extend(node.body)
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                if node.value is None:
                    continue
                # Only consider the edited expression. A lambda body does
                # not run when its dispatch table is constructed.
                stack = [node.value]
                while stack:
                    expr = stack.pop()
                    if isinstance(expr, ast.Lambda):
                        continue
                    if (isinstance(expr, (ast.Call, ast.BinOp, ast.Subscript, ast.Name))
                            and expr.lineno in changed_lines):
                        if isinstance(expr, ast.Call):
                            # Booleans are valid values for both sides of the same flag.
                            # A different callee, argument expression, or keyword stays distinct.
                            args = [*expr.args, *(kw.value for kw in expr.keywords)]
                            signature = tuple("bool" if isinstance(arg, ast.Constant) and
                                              type(arg.value) is bool else ast.dump(arg) for arg in args)
                            operations.add(("call", ast.dump(expr.func), signature,
                                            tuple(kw.arg for kw in expr.keywords)))
                        else:
                            operations.add(("value", ast.dump(expr)))
                    stack.extend(ast.iter_child_nodes(expr))
            elif isinstance(node, (ast.Import, ast.ImportFrom, ast.Raise, ast.Assert)):
                operations.add(("statement", ast.dump(node)))
            else:
                pending.extend(n for n in ast.iter_child_nodes(node) if isinstance(n, ast.stmt))
        return operations

    def file_impact(self, edit):
        path = edit.old or edit.new
        readers = self.files.get(path, 0)
        if not path.endswith(".py"):
            # An unobserved new data file can be found via a directory glob.
            if not edit.old or not edit.new:
                return self.all
            return (readers | self.unmeasured) if readers else self.all
        if not edit.old or not edit.new or edit.old != edit.new:
            return self.all
        src = self.model(path)
        try:
            changed = ast.parse(edit.apply(src.text))
            compile(changed, path, "exec")  # also checks illegal return/break and similar errors
        except (SyntaxError, ValueError):
            # Collection errors can propagate through shared test modules.
            return self.all

        if ast.dump(changed) == ast.dump(src.tree):
            return readers
        if len(edit.new_lines) + len(edit.touched) > 240:
            return self.all
        if (self.state and path.startswith("sqlglot/dialects/") and edit.touched and not edit.insertions
                and eligible(src, changed, edit.touched)):
            other = patched_state(self.source, [edit])
            mask = self.state.impact(other) if other is not None else None
            if mask is not None:
                return readers | mask | self.unmeasured | (self.all & ~self.state.complete)
        exact = None
        if self.precise and edit.touched and not edit.insertions:
            exact = self.precise.impact(path, src.tree, changed, edit.touched)
            if exact is not None:
                exact |= readers | self.unmeasured
                if not self.precise.includes_imports:
                    cold = self.imports.get(path, {})
                    exact |= union(cold.get(n, 0) for n in edit.touched)
        if self.profiles and edit.touched and not edit.insertions:
            measured = self.profiles.impact(path, src, changed, edit.touched, self.imports.get(path, {}))
            if measured is not None:
                bound = readers | measured | self.unmeasured | (self.all & ~self.profiles.complete)
                return bound if exact is None else bound & exact
        # Some built-in dialects parse SQL while being imported. Missing
        # required arguments there can prevent entire test modules loading.
        # A key with an unresolved cold read has no per-test validation bound.
        if path.startswith("sqlglot/expressions/"):
            for line in edit.touched | {offset + 1 for offset in edit.insertions}:
                for node in src.scope(line):
                    if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "arg_types"
                                                            for t in node.targets)):
                        return self.all
        new_ops = self.import_operations(changed, edit.new_lines)
        if new_ops and new_ops - self.import_operations(src.tree, edit.touched):
            return self.all
        result = readers | self.unmeasured
        identity = self.identity_change(path, src, changed)
        if identity is not None:
            return result | identity
        for line in edit.touched:
            result |= self.line_impact(path, src, line)
        for offset in edit.insertions:
            # Both sides matter at block boundaries.
            result |= self.line_impact(path, src, max(1, offset), True)
            result |= self.line_impact(path, src, offset + 1, True)
        return result if exact is None else result & exact

    def choose(self, patch):
        try:
            mask = union(self.file_impact(edit) for edit in parse_patch(patch))
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            mask = self.all
        mask &= self.all
        numbers = []
        while mask:
            bit = mask & -mask
            numbers.append(self.numbers[bit.bit_length() - 1])
            mask ^= bit
        return numbers


def schedule(numbers, durations):
    if not numbers:
        return []
    ordered = sorted(numbers, key=lambda n: (-durations[n], n))
    total = sum(durations[n] for n in ordered)
    longest = durations[ordered[0]]
    best_cost, best = total + 2000, [ordered]
    for count in range(2, min(8, len(ordered)) + 1):
        lower_bound = max(longest, (total + count - 1) // count) + 2000 * count
        if lower_bound >= best_cost:
            continue
        heap = [(0, i) for i in range(count)]
        workers = [[] for _ in heap]
        for n in ordered:
            load, i = heap[0]
            workers[i].append(n)
            heapq.heapreplace(heap, (load + durations[n], i))
        cost = max(load for load, _ in heap) + 2000 * count
        if cost < best_cost:
            best_cost, best = cost, workers
    return best


def schedule_cost(numbers, times):
    workers = schedule(numbers, times)
    return 2000 * len(workers) + max((sum(times[n] for n in w) for w in workers), default=0)


class Verifier:
    def __init__(self, index, impact, names, times):
        self.impact = impact
        self.names = names
        self.times = times
        self.inputs = index.with_name("call-inputs.json.gz")
        def read_optional(name, reader):
            path = index.with_name(name + ".json.gz")
            return reader(path) if path.is_file() else None
        self.checkpoints = read_optional("call-sites", Checkpoints) if self.inputs.is_file() else None
        shared = index.with_name("shared-fixtures.json")
        self.shared = json.loads(shared.read_bytes()) if shared.is_file() else {}
        self.parser_queries = read_optional("parser-cases", QueryCoverage)
        self.queries = read_optional("queries", QueryCoverage)
        self.fixtures = read_optional("optimizer-fixtures", QueryCoverage)
        impact.state = read_optional("dialect-state", lambda p: StateCoverage(p, impact.test_ids))

    def executor_plan(self, numbers, edits, deadline):
        nodes = [self.names[n] for n in numbers]
        cases = self.queries.plan(self.impact, edits, nodes) if self.queries else {}
        tpcds_check = None
        if self.checkpoints and self.queries and TPCDS in nodes and TPCDS not in cases:
            indices = self.checkpoints.plan(self.impact, edits)
            remaining = deadline - time.monotonic()
            if indices and remaining > 0.5:
                field, _, costs = self.queries.methods["test_execute_tpcds"]
                calls = [(i, self.checkpoints.data["owners"][i]) for i in indices]
                tpcds_check = (self.inputs, calls, costs, TPCDS, field)
        return cases, tpcds_check

    def refine(self, numbers, text, deadline):
        edits = parse_patch(text)
        if self.fixtures and all(e.old == e.new and e.old.startswith("tests/fixtures/")
                                 and e.old.removeprefix("tests/fixtures/") in self.shared for e in edits):
            passed = verify_fixtures(self.impact.source, edits, [self.names[n] for n in numbers],
                                     self.shared, self.fixtures.ranges, min(4.5, deadline - time.monotonic()))
            return [n for n in numbers if self.names[n] not in passed]
        if not all(e.old == e.new and e.old.startswith("sqlglot/") and e.old.endswith(".py") for e in edits):
            return numbers
        cases, tpcds_check = self.executor_plan(numbers, edits, deadline)
        times, names = self.times, self.names
        parser_test = "tests/test_parser.py::TestParser::test_max_nodes"
        if self.parser_queries and any(names[n] == parser_test for n in numbers):
            selection = self.parser_queries.parser_plan(self.impact, edits)
            if selection:
                cases[parser_test] = selection
        total = sum(times[n] for n in numbers)
        # Class-table substitutions and copy flags can affect many callers
        # without changing their assertions. Their inexpensive groups are
        # worth running even when they span more than one test module.
        def class_value(e, line):
            chain = self.impact.model(e.old).scope(line)
            return any(isinstance(n, ast.ClassDef) for n in chain) and not any(isinstance(n, FUNCTION) for n in chain)
        extended = (all(e.touched and all(class_value(e, line) for line in e.touched) for e in edits)
                    or bool(re.search(r"^[-+].*\bcopy=(True|False)", text, re.M)))
        nodes = []
        if (total <= 650 and len(numbers) <= 45) or (extended and total <= 6000 and len(numbers) <= 100):
            nodes = [names[n] for n in sorted(numbers, key=times.get)
                     if extended or names[n].startswith("tests/dialects/")]
        if self.fixtures and total < 12000:
            slices = self.fixtures.fixture_plan(self.impact, edits, [names[n] for n in numbers])
            cases.update(slices)
            if slices and sum(times[n] for n in numbers if names[n] not in slices) < 1100:
                nodes.extend(names[n] for n in numbers)
        leading = [TPCDS] if tpcds_check else []
        nodes = list(dict.fromkeys([*leading, *cases, *nodes]))
        if not nodes:
            return numbers
        potential = set(nodes)
        floor = [n for n in numbers if names[n] not in potential]
        gain = schedule_cost(numbers, times) - schedule_cost(floor, times)
        remaining = deadline - time.monotonic()
        if gain < 450 or remaining < 0.35:
            return numbers
        timeout = min(remaining, 6.0 if tpcds_check else (4.5 if cases or total > 650 else 1.75))
        passed = verify_commit(self.impact.source, edits, nodes, cases, timeout, tpcds_check=tpcds_check)
        return [n for n in numbers if names[n] not in passed]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--index", type=Path, default=Path(__file__).parent / "maps/coverage.json")
    parser.add_argument("--replay", action="store_true", help="execute cheap candidates on trusted changes")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_bytes())["tests"]
    impact = Impact(args.source, args.index, tests)
    times = {row["number"]: round(row["seconds"] * 1000) for row in tests}
    names = {t["number"]: t["id"] for t in tests}
    patches = sorted((args.data / "commits").glob("*.patch"))
    verifier = None
    if args.replay:
        warmup()
        verifier = Verifier(args.index, impact, names, times)
    budget = min(48.0, max(12.0, len(patches) * 0.8))
    for patch in patches:
        try:
            text = patch.read_text(encoding="utf-8")
            start = time.monotonic()
            if budget < 0.5:
                impact.state = None
            numbers = impact.choose(text)
            if verifier and numbers and budget > 0.5:
                try:
                    numbers = verifier.refine(numbers, text, start + budget)
                except (OSError, ValueError, RecursionError, SyntaxError):
                    pass
            budget -= time.monotonic() - start
        except UnicodeError:
            numbers = list(times)
        print(json.dumps({"commit": patch.stem, "workers": schedule(numbers, times)}, separators=(",", ":")))


if __name__ == "__main__":
    main()
