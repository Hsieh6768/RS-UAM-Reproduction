"""D012–D014 unique-passenger metrics and per-batch timing distributions."""
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import fsum
from statistics import fmean

import numpy as np


def sim_date(origin, tick):
    return (origin + timedelta(seconds=int(tick))).isoformat(sep=" ")


def inputs_origin(config):
    return datetime.fromisoformat(str(config["simulation"]["warmup_start"]))


def passenger_reports(path, snapshot, config):
    counts, modes, hours, days = Counter(), Counter(), defaultdict(Counter), defaultdict(Counter)
    durations, waits, detours = [], [], []
    period = config["simulation"]
    origin = inputs_origin(config)
    start = datetime.fromisoformat(str(period["evaluation_start"]))
    end = datetime.fromisoformat(str(period["evaluation_end_exclusive"]))
    fields = ("request_id source_record_id cohort original_request_time original_request_second orig_lon orig_lat "
              "dest_lon dest_lat mapped_origin mapped_dest party_size accept_time actual_first_pickup "
              "actual_final_dropoff state plan_id bindings service_mode base_direct_road_time_s actual_total_time_s "
              "detour_time_min is_in_eval final_counted_success unserved_reason").split()
    with (path / "requests.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for r in sorted(snapshot.requests.values(), key=lambda req: (req.submitted_at, req.request_id)):
            arrival = origin + timedelta(seconds=r.submitted_at)
            cohort = "WARMUP" if arrival < start else "TAIL" if arrival >= end else "EVALUATION"
            if r.cohort != cohort:
                raise ValueError(f"Final audit: request {r.request_id} has incorrect time cohort {r.cohort}")
            counts[cohort] += 1
            served = r.completed_at is not None
            total = r.completed_at - r.submitted_at if served else None
            delta = float((total - r.direct_road_time_s) / 60) if served else None
            mode = r.plan.service_mode if served else "Unserved"
            if cohort == "EVALUATION":
                modes[mode] += 1
                for group in (hours[arrival.strftime("%Y-%m-%d %H:00")], days[arrival.date().isoformat()]):
                    group["valid"] += 1
                    group["accepted"] += int(served)
                if served:
                    durations.append(total)
                    waits.append(r.first_pickup_at - r.submitted_at)
                    detours.append(delta)
            lon, lat = r.original_origin or (None, None)
            end_lon, end_lat = r.original_destination or (None, None)
            writer.writerow(dict(zip(fields, (
                r.request_id, r.request_id, cohort, sim_date(origin, r.submitted_at), r.submitted_at,
                lon, lat, end_lon, end_lat, r.origin, r.destination, r.party_size, r.accepted_at, r.first_pickup_at,
                r.completed_at, r.status.value, r.plan.plan_id if served else None, json.dumps(r.bindings), mode,
                r.direct_road_time_s, total, delta, cohort == "EVALUATION", cohort == "EVALUATION" and served,
                None if served else "EXPIRED_UNSERVED"))))
    evaluation = counts["EVALUATION"]
    for group in (*hours.values(), *days.values()):
        group["service_rate"] = group["accepted"] / group["valid"]
    modes = {mode: modes[mode] for mode in ("SHRS", "MHRS", "RS-UAM", "Unserved")}
    return {
        "cohorts": {c: counts[c] for c in ("WARMUP", "EVALUATION", "TAIL")}, "evaluation_requests": evaluation,
        "accepted_evaluation": len(durations), "completed_evaluation": len(durations),
        "expired_evaluation": modes["Unserved"], "service_rate": len(durations) / evaluation if evaluation else None,
        "mean_detour_min": fmean(detours) if detours else None,
        "mean_total_trip_min": fmean(durations) / 60 if durations else None,
        "mean_wait_min": fmean(waits) / 60 if waits else None,
        "negative_detour_count": sum(d < 0 for d in detours),
        "service_modes": modes, "service_mode_shares": {m: n / evaluation if evaluation else None
                                                       for m, n in modes.items()},
        "per_hour": dict(hours), "per_day": dict(days),
    }


def flight_report(path, snapshot):
    fields = ("flight_id route_id origin destination depart_at arrive_at capacity reserved_seats "
              "remaining_seats reservations status").split()
    departed_capacity = 0
    reserved = 0
    with (path / "flight_usage.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        for f in sorted(snapshot.flights.values(), key=lambda f: (f.depart_at, f.flight_id)):
            row = {k: getattr(f, k) for k in fields}
            row.update(reservations=json.dumps(dict(f.reservations), ensure_ascii=False), status=f.status.value)
            writer.writerow(row)
            if f.depart_at <= snapshot.time:
                departed_capacity += f.capacity
                reserved += f.reserved_seats
    return {"departed_capacity": departed_capacity, "departed_reserved": reserved,
            "seat_utilization": reserved / departed_capacity if departed_capacity else None}


def performance_report(batch_path, config):
    measurements, flags = defaultdict(list), Counter()
    op_uses, m5_status = Counter(), Counter()
    n = 0
    m4_cfg = config["m4"]
    limit = 1000 * m4_cfg["wall_budget_seconds"]
    with batch_path.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            n += 1
            m5_status[row["m5_status"]] += 1
            for key, val in row.items():
                if key.endswith(("_ms", "_bytes")) or key.startswith("fleet_") or key in (
                                                   "candidates_evaluated", "repair_count", "graph_components",
                                                   "largest_component_tasks", "decode_count", "m1_planned",
                                                   "m1_cache_hits", "operator_rewards", "evaluated_components",
                                                   "reused_components", "candidates_started", "m4_tasks"):
                    if val:
                        measurements[key].append(float(val))
            for key in ("budget_exhausted", "m4_wall_budget_exceeded", "batch_wall_budget_exceeded"):
                flags[key] += row[key] in ("True", "true", "1")
            if row.get("operator_draws"):
                op_uses.update(json.loads(row["operator_draws"]))
    dist = {}
    for key, values in measurements.items():
        p50, p95, p99 = np.percentile(values, [50, 95, 99])
        dist[key] = dict(count=len(values), mean=fmean(values), median=float(p50), p95=float(p95),
                         p99=float(p99), max=max(values), total=fsum(values))
    hits = sum(measurements["m1_cache_hits"])
    lookups = hits + sum(measurements["m1_planned"])
    return {
        **dist, **{key + "_rate": f / n for key, f in flags.items()}, "batch_count": n,
        "budget_clock": m4_cfg["budget_clock"], "m4_wall_reference_s": m4_cfg["wall_budget_seconds"],
        "m4_wall_max_overrun_ms": max(0, dist["m4_wall_ms"]["max"] - limit),
        "batch_wall_max_overrun_ms": max(0, dist["batch_wall_ms"]["max"] - limit),
        "flags_count": dict(flags), "operator_draws": dict(op_uses), "m5_status_counts": dict(m5_status),
        "m1_cache_hit_rate": hits / lookups if lookups else None,
    }


def report_markdown(summary, audit, perf):
    def fmt(value):
        return "无样本" if value is None else f"{value:.4f}"

    lines = [
        "# 滚动调度实验报告", "",
        f"运行 `{summary['run_id']}` 在模拟时间 {summary['last_simulated_datetime']} 结束。", "",
        "| 评价请求 | 合法接受 | 完成 | 过期未服务 | 服务率 |", "|---:|---:|---:|---:|---:|",
        f"| {summary['evaluation_requests']} | {summary['accepted_evaluation']} | {summary['completed_evaluation']} "
        f"| {summary['expired_evaluation']} | {fmt(summary['service_rate'])} |", "",
        f"平均完整出行 {fmt(summary['mean_total_trip_min'])} 分钟，平均等候 {fmt(summary['mean_wait_min'])} 分钟，"
        f"平均绕行 {fmt(summary['mean_detour_min'])} 分钟。负绕行请求 {summary['negative_detour_count']} 条。", "",
        "| 服务模式 | 评价请求数 |", "|---|---:|",
    ]
    lines.extend(f"| {k} | {v} |" for k, v in summary["service_modes"].items())
    lines.extend(["", "| 计时项 | 均值 ms | p95 ms | p99 ms | 最大 ms |", "|---|---:|---:|---:|---:|"])
    for name in ("m1_wall_ms", "m3_wall_ms", "m4_wall_ms", "m4_cpu_ms", "m5_wall_ms", "m5_cpu_ms",
                 "validator_wall_ms", "batch_wall_ms"):
        stats = perf[name]
        lines.append(f"| {name} | {fmt(stats['mean'])} | {fmt(stats['p95'])} | {fmt(stats['p99'])} | {fmt(stats['max'])} |")
    lines.extend(["", f"资源审计 {audit['status']}。共复核 {audit['event_rows']} 条事件、"
                  f"{audit['task_count']} 项乘客任务及 {audit['flight_seats_reserved']} 个预订席位。", "",
                  "输入身份见 manifest.json，逐批预算和计算时间见 performance.json。"])
    return "\n".join(lines) + "\n"
