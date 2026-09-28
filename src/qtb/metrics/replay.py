"""Exact C6 routing replay with elided init permutations.

A ``mismatch`` carries a ``witness``: where and in which state the replay failed, precise
enough that two failures with the same witness are the same failure (``failure_witness``).
The routed-prefix digest is computed only on the failure path.
"""

from collections import defaultdict, deque

from qtb.canonical import canonical_bytes, digest
from qtb.metrics import CONTROL_FLOW, layout_errors


def replay(logical, routed, layout, input_width, output_width, elided=None):
    errors = layout_errors(layout, input_width, output_width)
    if errors:
        return {
            "status": "mismatch",
            "detail": errors,
            "witness": {"kind": "layout", "errors": errors, "layout": digest(layout)},
        }
    initial = layout["initial_index_layout"] if layout else list(range(output_width))
    final = layout["final_index_layout"] if layout else list(range(output_width))
    elided = list(elided or range(input_width)) + list(range(input_width, output_width))
    if sorted(elided) != list(range(output_width)):
        return {
            "status": "mismatch",
            "detail": "Invalid elided permutation",
            "witness": {"kind": "elided", "elided": digest(elided)},
        }
    ops, queues, signatures = [], defaultdict(deque), set()
    for op in logical:
        name, qs, cs, params, payload = op
        if name == "barrier":
            continue
        if name in CONTROL_FLOW or not qs and not cs:
            return {"status": "unverified", "detail": "Outside static replay model"}
        index = len(ops)
        ops.append(op)
        signatures.add(canonical_bytes([name, params, payload]))
        for wire in [("q", q) for q in qs] + [("c", c) for c in cs]:
            queues[wire].append(index)
    p2v = [0] * output_width
    for v, p in enumerate(initial):
        p2v[p] = v
    swaps = 0

    def witness(kind, position, operation=None):
        return {
            "kind": kind,
            "position": position,
            "operation": None if operation is None else digest(operation),
            "mapping": digest(p2v),
            "prefix": digest(routed[:position]),
            "initial": digest(initial),
            # The init snapshot can itself differ between revisions. The same routed
            # operation/mapping is not the same failure against different expectations.
            "logical": digest(logical),
            "elided": digest(elided),
        }

    for position, (name, ps, cs, params, payload) in enumerate(routed):
        if name == "barrier":
            continue
        if any(type(p) is not int or not 0 <= p < output_width for p in ps):
            return {
                "status": "mismatch",
                "detail": "Invalid routed wire",
                "witness": witness("wire", position, routed[position]),
            }
        vs = [p2v[p] for p in ps]
        wires = [("q", v) for v in vs] + [("c", c) for c in cs]
        if name in CONTROL_FLOW or not wires:
            return {"status": "unverified", "detail": "Outside static replay model"}
        heads = [queues[w][0] if queues[w] else None for w in wires]
        if (
            heads[0] is not None
            and len(set(heads)) == 1
            and ops[heads[0]] == [name, vs, cs, params, payload]
        ):
            for wire in wires:
                queues[wire].popleft()
        elif name == "swap" and len(ps) == 2 and not cs and not params and payload is None:
            a, b = ps
            p2v[a], p2v[b] = p2v[b], p2v[a]
            swaps += 1
        else:
            status = (
                "mismatch"
                if canonical_bytes([name, params, payload]) in signatures
                else "unverified"
            )
            result = {"status": status, "detail": "Operation cannot be replayed", "operation": name}
            if status == "mismatch":
                result["witness"] = witness("operation", position, [name, vs, cs, params, payload])
            return result
    leftover = sorted(min(queue) for queue in queues.values() if queue)
    ok = not leftover and all(p2v[final[v]] == elided[v] for v in range(output_width))
    result = {
        "status": "verified" if ok else "mismatch",
        "routing_swaps": swaps,
        "covers": ["layout", "routing"],
        "substituted": [],
    }
    if not ok:
        result["detail"] = "Unreplayed operations" if leftover else "Final mapping differs"
        result["witness"] = dict(
            witness("final", len(routed)), leftover=leftover[:10], final=digest(final)
        )
    return result
