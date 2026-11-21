"""Per-run streaming files, terminal status and failure evidence for P6."""
import csv
import gzip
import hashlib
import json
import platform
import subprocess
import sys
import traceback
from collections import Counter
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import yaml

from src.data.sources import fingerprint
from src.evaluation.audit import audit_world
from src.evaluation.metrics import (
    flight_report, inputs_origin, passenger_reports, performance_report, report_markdown, sim_date,
)


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(v) for key, v in value.items()}
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _write_json(path, data):
    pending = path.with_suffix(".json.tmp")
    with pending.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(_plain(data), handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")
    pending.replace(path)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _git(root, args):
    try:
        result = subprocess.run(["git", "--no-optional-locks", *args], cwd=root, capture_output=True,
                                timeout=5, check=True)
        return result.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _code_identity():
    root = Path(__file__).resolve().parents[2]
    # A commit alone does not identify the active implementation in a dirty worktree.
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(bytes.fromhex(fingerprint(path)["sha256"]))
    status = _git(root, ["status", "--porcelain"])
    return {"code_commit": _git(root, ["rev-parse", "HEAD"]),
            "dirty_tree": None if status is None else bool(status), "source_tree_sha256": digest.hexdigest()}


class RunOutput:
    """One writer and one directory per invocation. COMPLETE is the last durable update."""

    def __init__(self, output_root, config, manifest):
        self.config = _plain(config)
        self.origin = inputs_origin(config)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
        self.path = Path(output_root) / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        config_file = self.path / "config.yaml"
        config_file.write_text(yaml.safe_dump(self.config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.manifest = {
            **_code_identity(), "run_id": run_id, "start_wall": _now(), "status": "RUNNING",
            "python_version": platform.python_version(), "python_executable": sys.executable,
            "environment": Path(sys.prefix).name, "os": platform.platform(), "cpu_model": platform.processor(),
            "gurobi_version": version("gurobipy"), "configs_sha256": fingerprint(config_file)["sha256"],
            "method_deviations": ["D002", "D004", "D008", "D010", "D011", "D012", "D013", "D014"],
            **_plain(manifest),
        }
        _write_json(self.path / "manifest.json", self.manifest)
        self._log = (self.path / "console.log").open("x", encoding="utf-8", buffering=1)
        self._batches = (self.path / "batches.csv").open("x", newline="", encoding="utf-8")
        self._writer = None
        self._events = gzip.open(self.path / "vehicle_events.csv.gz", "xt", encoding="utf-8", newline="")
        self._event_writer = csv.writer(self._events)
        self._event_writer.writerow(("time", "kind", "seq", "object_id", "vehicle_id", "route_version", "source",
                                    "target", "way_id", "occurrence", "length_m", "duration_s", "physical_time",
                                    "road_purpose"))
        self.last_batch = {}
        self.totals = Counter()

    def update_manifest(self, fields):
        self.manifest.update(_plain(fields))
        _write_json(self.path / "manifest.json", self.manifest)

    def events(self, events):
        for e in events:
            edge = e.edge
            vehicle = e.object_id if e.kind == "EDGE_END" else e.vehicle_id
            self._event_writer.writerow((
                e.time, e.kind.value, e.seq, e.object_id, vehicle, e.version, edge.source if edge else None,
                edge.target if edge else None, edge.way_id if edge else None, edge.occurrence if edge else None,
                edge.length_m if edge else None, edge.time_s if edge else None, e.physical_time,
                getattr(e, "road_purpose", None),
            ))

    def _print(self, line):
        self._log.write(line + "\n")
        self._log.flush()
        print(line, flush=True)

    def batch(self, row):
        if self.last_batch and (row["batch"] != self.last_batch["batch"] + 1
                                or row["batch_time"] <= self.last_batch["batch_time"]):
            raise ValueError("Output batch number or simulated time is out of order")
        data = _plain(row)
        data["sim_datetime"] = sim_date(self.origin, row["batch_time"])
        if self._writer is None:
            self._writer = csv.DictWriter(self._batches, list(data))
            self._writer.writeheader()
        if set(data) != set(self._writer.fieldnames):
            raise ValueError("Inconsistent batch CSV fields")
        self._writer.writerow({k: json.dumps(v, ensure_ascii=False, allow_nan=False) if isinstance(v, (dict, list))
                               else v for k, v in data.items()})
        self._batches.flush()
        self._events.flush()
        self._print(("[{sim_datetime}] batch={batch} {cohort} new={new_req} pending={pending_after} "
                     "accepted={accepted_new} expired={expired} in_service={accepted_incomplete} "
                     "fleet(idle/loaded/pickup/rebalance)={fleet_idle}/{fleet_loaded}/{fleet_pickup}/{fleet_rebalancing} "
                     "M4={m4_wall_ms:.1f}ms cpu={m4_cpu_ms:.1f}ms evals={candidates_evaluated} "
                     "budget_exhausted={budget_exhausted} M5={m5_status} rebalanced={rebalanced} "
                     "total={batch_wall_ms:.1f}ms EVAL(accepted/completed)={evaluation_accepted}/{evaluation_completed}"
                     ).format(**data))
        self.last_batch = data
        for key in ("new_req", "accepted_new", "expired"):
            self.totals[key] += row[key]

    def complete(self, snapshot):
        self._batches.close()
        self._events.close()
        event_file = self.path / "vehicle_events.csv.gz"
        audit = audit_world(snapshot, event_file, self.totals, self.last_batch,
                            self.manifest.get("expected_evaluation_requests"),
                            self.manifest.get("initial_fleet_nodes"))
        _write_json(self.path / "audit.json", audit)
        perf = performance_report(self.path / "batches.csv", self.config)
        _write_json(self.path / "performance.json", perf)
        summary = {
            **passenger_reports(self.path, snapshot, self.config), "flights": flight_report(self.path, snapshot),
            "road_metres": audit["road_metres"], "performance": perf,
            "status": "COMPLETE", "run_id": self.manifest["run_id"],
            "batch_count": perf["batch_count"], "last_simulated_datetime": sim_date(self.origin, snapshot.time),
            "last_simulated_second": snapshot.time, "final_audit": audit["status"],
        }
        (self.path / "summary.md").write_text(report_markdown(summary, audit, perf), encoding="utf-8")
        _write_json(self.path / "summary.json", summary)
        self._log.close()
        checksums = {p.name: fingerprint(p) for p in sorted(self.path.iterdir())
                     if p.is_file() and p.name != "manifest.json"}
        self.update_manifest({"status": "COMPLETE", "end_wall": _now(), "output_checksums": checksums})
        return summary

    def fail(self, exception, snapshot=None, batch=None, *, phase="dispatch"):
        interrupted = isinstance(exception, KeyboardInterrupt)
        status = "INTERRUPTED" if interrupted else "FAILED"
        self._events.close()
        self._batches.close()
        detail = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
        last = self.last_batch.get("batch")
        _write_json(self.path / "failure.json", {
            "status": status, "phase": phase, "batch": batch if batch is not None else (last or 0) + 1,
            "last_successful_batch": last, "exception": str(exception), "traceback": detail, "snapshot": snapshot,
        })
        if self._log.closed:
            self._log = (self.path / "console.log").open("a", encoding="utf-8")
        self._print(f"{status} {type(exception).__name__}: {exception}")
        self._log.close()
        # Failure can occur during final report production (e.g. checksum I/O).
        for name in ("summary.json", "summary.md"):
            (self.path / name).unlink(missing_ok=True)
        _write_json(self.path / "audit.json", {"status": status, "cause": str(exception)})
        self.update_manifest({"status": status, "end_wall": _now(), "last_successful_batch": last})
