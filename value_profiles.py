"""Use observed baseline inputs to refine changes to configuration and comparisons."""
import ast
from functools import lru_cache
import json
import operator
from comparison_relations import OPS, changed_mask


COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
           ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def literal(node):
    try:
        return True, ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return False, None


def equal(a, b):
    return ast.dump(a) == ast.dump(b)


def paired_nodes(old, new, lines):
    """Only align nodes down matching field paths. An enclosing AST check follows."""
    if type(old) is not type(new):
        return
    if hasattr(old, "lineno") and not any(old.lineno <= n <= old.end_lineno for n in lines):
        return
    if isinstance(old, (ast.Dict, ast.Compare, ast.Assign, ast.AnnAssign)):
        yield old, new
    for key, left in ast.iter_fields(old):
        right = getattr(new, key)
        if isinstance(left, ast.AST) and isinstance(right, ast.AST):
            yield from paired_nodes(left, right, lines)
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for a, b in zip(left, right):
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    yield from paired_nodes(a, b, lines)


class Profiles:
    def __init__(self, filename, names, source):
        data = json.loads(filename.read_bytes())
        if data["tests"] != names:
            raise ValueError("coverage and value profiles use different test IDs")
        masks = [int(m, 16) for m in data["masks"]]
        self.relations = data.get("relations", {})
        self.masks = masks
        self.fields = {s: masks[m] for s, m in data.get("fields", {}).items()}
        self.entries = {site: masks[m] for site, m in data["entries"].items()}
        self.scalars = {site: {v: masks[m] for v, m in rows.items()}
                        for site, rows in data["scalars"].items()}
        self.complete = masks[data["complete"]]
        self.source = source

    @lru_cache(maxsize=128)
    def closure_factory(self, path, name, follow=True):
        """Recognize factories whose only eager work is creating a function."""
        tree = ast.parse((self.source / path).read_bytes())
        for node in tree.body:
            if follow and isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot."):
                for alias in node.names:
                    if (alias.asname or alias.name) == name:
                        file = node.module.replace(".", "/") + ".py"
                        if (self.source / file).is_file():
                            return self.closure_factory(file, alias.name, False)
            if isinstance(node, ast.FunctionDef) and node.name == name and not node.decorator_list:
                body = list(node.body)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    body = body[1:]
                if len(body) == 1 and isinstance(body[0], ast.Return):
                    fn = body[0].value
                    return isinstance(fn, ast.Lambda) and not (fn.args.defaults or fn.args.kwonlyargs)
                if len(body) == 2 and isinstance(body[0], ast.FunctionDef) and isinstance(body[1], ast.Return):
                    fn, ret = body[0], body[1].value
                    return (isinstance(ret, ast.Name) and ret.id == fn.name and
                            not (fn.decorator_list or fn.args.defaults or fn.args.kwonlyargs))
        return False

    def eligible_value(self, path, old, new):
        if literal(old)[0] and literal(new)[0]:
            return True
        if isinstance(old, ast.Lambda) and isinstance(new, ast.Lambda):
            return equal(old.args, new.args)
        if isinstance(old, ast.Call) and isinstance(new, ast.Call) and isinstance(old.func, ast.Name):
            return (equal(old.func, new.func) and len(old.args) == len(new.args)
                    and [x.arg for x in old.keywords] == [x.arg for x in new.keywords]
                    and all(literal(x)[0] for call in (old, new) for x in
                            [*call.args, *(kw.value for kw in call.keywords)])
                    and self.closure_factory(path, old.func.id))
        return False

    def compare(self, site, old, new, cold):
        if (site in self.relations and len(old.ops) == len(new.ops) == 1
                and type(old.ops[0]) in OPS and type(new.ops[0]) in OPS
                and type(old.ops[0]) is not type(new.ops[0]) and equal(old.left, new.left)
                and equal(old.comparators[0], new.comparators[0])):
            return cold | changed_mask(self.relations[site], old.ops[0], new.ops[0], self.masks)
        if (len(old.ops) != 1 or len(new.ops) != 1 or type(old.ops[0]) not in COMPARE
                or type(new.ops[0]) not in COMPARE or not equal(old.left, new.left)):
            return None
        if type(old.ops[0]) is type(new.ops[0]) and equal(old.comparators[0], new.comparators[0]):
            return None
        if site not in self.scalars:
            return None
        a_ok, a = literal(old.comparators[0])
        b_ok, b = literal(new.comparators[0])
        if not (a_ok and b_ok):
            return None
        affected = cold
        for raw, mask in self.scalars[site].items():
            if raw == "?":
                affected |= mask
                continue
            value = ast.literal_eval(raw)
            try:
                if COMPARE[type(old.ops[0])](value, a) != COMPARE[type(new.ops[0])](value, b):
                    affected |= mask
            except (TypeError, ValueError):
                affected |= mask
        return affected

    def impact(self, path, src, changed, lines, imports):
        """Return a bound only if these substitutions account for the whole file edit."""
        undo = []
        affected = 0
        try:
            # Materialize pairs before substituting to avoid descending into base nodes.
            for old, new in list(paired_nodes(src.tree, changed, lines)):
                if isinstance(old, ast.Compare):
                    site = f"{path}:{old.lineno}:{old.col_offset}"
                    mask = self.compare(site, old, new, imports.get(old.lineno, 0))
                    if mask is not None:
                        affected |= mask
                        undo.extend([(new.ops, 0, new.ops[0]), (new.comparators, 0, new.comparators[0])])
                        new.ops[0], new.comparators[0] = old.ops[0], old.comparators[0]
                elif isinstance(old, (ast.Assign, ast.AnnAssign)) and isinstance(src.parents.get(old), ast.ClassDef):
                    targets = old.targets if isinstance(old, ast.Assign) else [old.target]
                    if (len(targets) != 1 or not isinstance(targets[0], ast.Name)
                            or old.value is None or new.value is None):
                        continue
                    classes = []
                    scope = src.parents.get(old)
                    while isinstance(scope, ast.ClassDef):
                        classes.append(scope.name)
                        scope = src.parents.get(scope)
                    site = ".".join([path[:-3].replace("/", "."), *reversed(classes), targets[0].id])
                    if (site in self.fields and not isinstance(old.value, ast.Dict) and literal(old.value)[0] and literal(new.value)[0]
                            and not equal(old.value, new.value)):
                        affected |= self.fields[site]
                        undo.append((new, "value", new.value))
                        new.value = old.value
                elif isinstance(old, ast.Dict) and len(old.keys) == len(new.keys):
                    # A reference also evaluated in this same base dictionary
                    # was resolvable during construction (e.g. an enum member).
                    def reference(expr):
                        while isinstance(expr, ast.Attribute):
                            expr = expr.value
                        return isinstance(expr, ast.Name)
                    available = {ast.dump(value) for value in old.values if reference(value)}
                    for i, (key, a, b) in enumerate(zip(old.keys, old.values, new.values)):
                        if key is None or not any(a.lineno <= n <= a.end_lineno for n in lines):
                            continue
                        site = f"{path}:{key.lineno}:{key.col_offset}"
                        ref_swap = reference(a) and reference(b) and ast.dump(b) in available
                        if site in self.entries and not equal(a, b) and (ref_swap or self.eligible_value(path, a, b)):
                            affected |= self.entries[site]
                            undo.append((new.values, i, b))
                            new.values[i] = a
            if undo and equal(src.tree, changed):
                return affected
            return None
        finally:
            for seq, index, value in reversed(undo):
                if isinstance(seq, list):
                    seq[index] = value
                else:
                    setattr(seq, index, value)
