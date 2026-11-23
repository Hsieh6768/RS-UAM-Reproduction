"""Summarize archived HALNS request traces without running a solver."""

import argparse
from collections import Counter
import csv
from hashlib import sha256
import gzip
from io import BytesIO, TextIOWrapper
import json
from math import isclose
from pathlib import Path


OPERATORS = ("s1", "p_exchange", "v_exchange", "restart", "insert", "cut", "swap", "reverse")


def summarize_batch(stream, operator_totals):
    baseline = json.loads(next(stream))
    if baseline["ordinal"] != 0 or baseline["operator"] != "s0":
        raise ValueError("Missing S0")
    best = baseline["decoded"]["v"]
    current = 0
    started = considered = 0
    repairs, failures = Counter(), Counter()
    for ordinal, line in enumerate(stream, start=1):
        row = json.loads(line)
        if row["ordinal"] != ordinal or row["from_current"] != current:
            raise ValueError(f"Candidate chain mismatch at {ordinal}")
        total = operator_totals[row["operator"]]
        counts = total["counts"]
        started += 1
        counts["called"] += 1
        counts["generated"] += "encoded" in row
        counts["decoded"] += "decoded" in row
        reasons = [repair["reason"] for repair in row.get("repairs", ())]
        if "accepted_current" not in row:
            total["incomplete_repairs"].update(reasons)
            continue
        considered += 1
        counts["considered"] += 1
        outcome = row["decoded"]
        counts["feasible"] += outcome["feasible"]
        counts["target_retained"] += len(outcome["retained"])
        counts["accepted"] += row["accepted_current"]
        if row["accepted_current"]:
            current = ordinal
        repairs.update(reasons)
        total["repairs"].update(reasons)
        if outcome["failure"] is not None:
            reason = outcome["failure"]["reason"]
            failures[reason] += 1
            total["failures"][reason] += 1
        if row["became_best"] != (outcome["feasible"] and outcome["v"] > best):
            raise ValueError(f"Best flag mismatch at {ordinal}")
        if row["became_best"]:
            counts["best_improvements"] += 1
            total["best_gain"] += outcome["v"] - best
            best = outcome["v"]
    return started, considered, repairs, failures, best


def summarize(run):
    directory = run / "request_diagnostics"
    audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
    with (run / "batches.csv").open(encoding="utf-8", newline="") as stream:
        batches = {int(row["batch"]): row for row in csv.DictReader(stream)}
    operators = {
        op: {
            "counts": Counter(dict.fromkeys((
                "called", "generated", "decoded", "considered", "feasible",
                "target_retained", "accepted", "best_improvements",
            ), 0)),
            "repairs": Counter(),
            "incomplete_repairs": Counter(),
            "failures": Counter(),
            "best_gain": 0.,
        }
        for op in OPERATORS
    }
    covered = []
    wall_ms = cpu_ms = 0.
    for name, entry in sorted(audit["files"].items()):
        batch = entry["batch"]
        payload = (directory / name).read_bytes()
        if sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError(f"Trace checksum mismatch for {name}")
        with gzip.GzipFile(fileobj=BytesIO(payload)) as zipped:
            with TextIOWrapper(zipped, encoding="utf-8") as stream:
                header = json.loads(next(stream))
                if header["batch"] != batch or header["identity"] != audit["identity"]:
                    raise ValueError(f"Trace identity mismatch for {name}")
                started, considered, repairs, failures, best = summarize_batch(stream, operators)
        logged = batches[batch]
        if (started != int(logged["candidates_started"])
                or considered != int(logged["candidates_evaluated"])
                or sum(repairs.values()) != int(logged["repair_count"])
                or sum(failures.values()) != int(logged["invalid_candidates"])
                or repairs != Counter(json.loads(logged["repair_reasons"]))
                or failures != Counter(json.loads(logged["failure_reasons"]))
                or not isclose(best, float(logged["best_score"]), rel_tol=0., abs_tol=1e-9)):
            raise ValueError(f"Trace and batches.csv disagree for batch {batch}")
        covered.append(batch)
        wall_ms += float(logged["m4_wall_ms"])
        cpu_ms += float(logged["m4_cpu_ms"])

    if not covered or len(set(covered)) != len(covered) or len(covered) != audit["batch_count"]:
        raise ValueError("Trace batch coverage mismatch")
    return {
        "run_id": audit["identity"]["run_id"],
        "trace_audit_status": audit["status"],
        "covered_batches": covered,
        "all_batch_count": len(batches),
        "m4_wall_ms": wall_ms,
        "m4_cpu_ms": cpu_ms,
        "operators": operators,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Existing experiment output directory")
    print(json.dumps(summarize(parser.parse_args().run), indent=2))
