"""Interpret baseline scalar ordering and object identity for a changed comparison."""
import ast

OPS = {ast.Eq: "eq", ast.NotEq: "ne", ast.Lt: "lt", ast.LtE: "le",
       ast.Gt: "gt", ast.GtE: "ge", ast.Is: "is", ast.IsNot: "isnot"}


def outcome(state, op):
    if state == "?":
        return None
    identity, relation = state[0] == "1", state[1:]
    if op == "is":
        return identity
    if op == "isnot":
        return not identity
    if op == "eq":
        return relation in ("=", "equal")
    if op == "ne":
        return relation not in ("=", "equal")
    if relation in ("equal", "different"):
        return None
    return relation in {"lt": ("<",), "gt": (">",), "le": ("<", "="), "ge": (">", "=")}[op]


def changed_mask(rows, old, new, masks):
    before, after = OPS[type(old)], OPS[type(new)]
    result = 0
    for state, index in rows.items():
        a, b = outcome(state, before), outcome(state, after)
        if a is None or b is None or a != b:
            result |= masks[index]
    return result
