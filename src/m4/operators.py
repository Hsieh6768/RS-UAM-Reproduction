"""PAPER §5.3.4–5, Table 3. All moves act on candidate encodings only."""
from src.m2.passengers import Leg
from .control import OPERATORS
from .encoding import Choice, Encoding, pending_formal, task_chain


def _rows(decoder, plan):
    return tuple(decoder.rows.get((plan.request_id, plan.plan_id, step.leg_id), ())
                 for step in plan.legs if isinstance(step, Leg))


def _plans_with_resources(decoder, rid, excluding=None):
    available = []
    for plan in decoder.options.get(rid, ()):
        if plan.plan_id != excluding:
            rows = _rows(decoder, plan)
            if all(rows):
                available.append((plan, rows))
    return available


def _draw_choice(plan, rows, rng):
    return Choice(plan.plan_id, tuple(rng.choice(row) for row in rows))


def _task_count(plan, choice):
    return len(pending_formal(task_chain(plan, choice)))


def _adjust_count(sequence, rid, count, rng):
    positions = [i for i, r in enumerate(sequence) if r == rid]
    if count < len(positions):
        for i in sorted(rng.sample(positions, len(positions) - count), reverse=True):
            sequence.pop(i)
    else:
        for _ in range(count - len(positions)):
            sequence.insert(rng.randrange(len(sequence) + 1), rid)


def _exchange_resources(rows, old, rng):
    """Uniform draw from the Cartesian product excluding the current binding.

    Rank each binding in mixed radix so a single draw suffices even for a
    multi-leg plan with many vehicles. Skip the rank of the current tuple.
    """
    size, old_rank = 1, 0
    for row, resource in zip(rows, old.resources):
        size *= len(row)
        old_rank = old_rank * len(row) + row.index(resource)
    rank = rng.randrange(size - 1)
    if rank >= old_rank:
        rank += 1
    selected = []
    for row in reversed(rows):
        rank, index = divmod(rank, len(row))
        selected.append(row[index])
    return Choice(old.plan_id, tuple(reversed(selected)))


def _exchange(name, decoder, encoding, rng, m, checkpoint, on_move):
    choices = dict(encoding.choices)
    sequence = list(encoding.sequence)
    alternatives = {}
    for rid in sorted(choices.keys() - decoder.fixed_choices.keys()):
        if checkpoint:
            checkpoint()
        old = choices[rid]
        if name == "p_exchange":
            plans = _plans_with_resources(decoder, rid, excluding=old.plan_id)
            if plans:
                alternatives[rid] = plans
        else:
            rows = _rows(decoder, decoder.selected_plan(rid, old))
            if any(len(row) > 1 for row in rows):
                alternatives[rid] = rows
    for rid in rng.sample(tuple(alternatives), min(m, len(alternatives))):
        if checkpoint:
            checkpoint()
        if name == "p_exchange":
            plan, rows = rng.choice(alternatives[rid])
            choices[rid] = _draw_choice(plan, rows, rng)
            _adjust_count(sequence, rid, _task_count(plan, choices[rid]), rng)
        else:
            choices[rid] = _exchange_resources(alternatives[rid], choices[rid], rng)
        if on_move is not None:
            on_move((rid,))
    return Encoding(choices, sequence)


def _restart(decoder, rng, checkpoint, on_move):
    base = decoder.safe_baseline.encoding
    choices = dict(base.choices)
    sequence = list(base.sequence)
    counts = {}
    for rid, typ in sorted(decoder.snapshot.request_types().items()):
        if checkpoint:
            checkpoint()
        req = decoder.snapshot.requests[rid]
        if typ == 3 or not req.submitted_at <= decoder.snapshot.time <= req.pickup_deadline:
            continue
        plans = _plans_with_resources(decoder, rid)
        if plans:
            plan, rows = rng.choice(plans)
            choices[rid] = _draw_choice(plan, rows, rng)
            counts[rid] = _task_count(plan, choices[rid])
            if on_move is not None:
                on_move((rid,))
    # §5.3.4 samples outstanding requests uniformly at every draw. Swap out
    # an exhausted ID in constant time while preserving seeded reproducibility.
    active = list(counts)
    while active:
        if checkpoint:
            checkpoint()
        index = rng.randrange(len(active))
        rid = active[index]
        sequence.append(rid)
        counts[rid] -= 1
        if counts[rid] == 0:
            active[index] = active[-1]
            active.pop()
    return Encoding(choices, sequence)


def _reorder(name, segment, rng, on_move):
    if len(segment) < 2:
        return
    if name == "insert":
        # A4: every pending occurrence is eligible. Decoding checks commitments.
        rid = segment.pop(rng.choice(range(len(segment))))
        segment.insert(rng.randrange(len(segment) + 1), rid)
        if on_move is not None:
            on_move((rid,))
        return
    first, last = sorted(rng.sample(range(len(segment)), 2))
    if first == last:
        return
    if name == "swap":
        if on_move is not None:
            on_move((segment[first], segment[last]))
        segment[first], segment[last] = segment[last], segment[first]
    elif name == "reverse":
        if on_move is not None:
            on_move(segment[first:last + 1])
        segment[first:last + 1] = reversed(segment[first:last + 1])
    else:  # cut: move the inclusive block to the end, keeping its order.
        block = segment[first:last + 1]
        if on_move is not None:
            on_move(block)
        del segment[first:last + 1]
        segment.extend(block)


def apply_operator(name, decoder, encoding, rng, *, fragments, m=1, checkpoint=None, on_move=None):
    """Return a raw candidate. The caller decodes, repairs and tests acceptance.

    Each fragment contains its increasing positions in the current layer-two
    sequence. Algorithm 4 lines 15–21 independently perturb every fragment
    and recover their original interleaving. noD supplies one global fragment.
    """
    if name not in OPERATORS:
        raise ValueError(f"Unknown HALNS operator: {name!r}")
    if type(m) is not int or m < 1:
        raise ValueError("Exchange request count m must be a positive integer")
    if name == "restart":
        return _restart(decoder, rng, checkpoint, on_move)
    if name in ("p_exchange", "v_exchange"):
        return _exchange(name, decoder, encoding, rng, m, checkpoint, on_move)
    sequence = list(encoding.sequence)
    for positions in fragments:
        if checkpoint:
            checkpoint()
        segment = [sequence[i] for i in positions]
        _reorder(name, segment, rng, on_move)
        for i, rid in zip(positions, segment):
            sequence[i] = rid
    return Encoding(encoding.choices, sequence)
