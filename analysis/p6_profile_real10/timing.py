"""Offline summaries for the existing P6 diagnostic log. Durations are milliseconds."""
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "outputs/20261121T151519Z-1ccd3331b2fa425e99cbfafde128809e"
OUT = Path(__file__).resolve().parent


def stats(values):
    v = pd.Series(values, dtype=float)
    return dict(n=len(v), total=v.sum(), mean=v.mean(), p50=v.median(),
                p95=v.quantile(.95), p99=v.quantile(.99), maximum=v.max())


def counter(series):
    values = Counter()
    for cell in series:
        values.update(json.loads(cell))
    return dict(values)


def mapping(value):
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def main():
    b = pd.read_csv(RUN / "batches.csv").set_index("batch")
    req = pd.read_csv(RUN / "requests.csv", dtype={"request_id": str}).set_index("request_id")
    p = [json.loads(line) for line in (RUN / "profiling.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["batch"] for r in p] == list(b.index[::10])
    stages, sampled_requests, sampling = [], [], []
    for row in p:
        batch = row["batch"]
        tick = row["second"]
        assert row["candidates_started"] == b.at[batch, "candidates_started"]
        for name, data in row["stages"].items():
            stages.append(dict(batch=batch, phase=b.at[batch, "cohort"], stage=name, calls=data["calls"],
                               inclusive_ms=data["inclusive_ns"]/1e6,
                               exclusive_ms=data["exclusive_ns"]/1e6))
        for sample in row["requests"]:
            r = req.loc[sample["request_id"]]
            actual_options = sum(plan["all_legs_have_resources"] for plan in sample["plans"])
            assert bool(actual_options) == sample["has_binding_options"]
            sampled_requests.append(dict(batch=batch, batch_cohort=b.at[batch, "cohort"],
                request_cohort=r.cohort, request_id=sample["request_id"], age_s=tick-sample["original_second"],
                plan_count=sample["plan_count"], complete_option_plans=actual_options,
                budget_exhausted=row["budget_exhausted"], final_run_state=r.state,
                **{k:sample[k] for k in ("has_binding_options","attempts","completed_attempts","retained_feasible",
                "removed_on_conflict","first_conflict","final_state","outcome")}))
        sampling.append({**{k:row[k] for k in ("batch","candidates_started","candidates_evaluated","sampled_candidates",
                          "candidate_samples_truncated","detail_spans","detail_spans_truncated","requests_truncated")},
                         "sampled_requests":len(row["requests"])})
    stages = pd.DataFrame(stages)
    output = []
    for (phase,name), group in pd.concat([stages, stages.assign(phase="ALL")]).groupby(["phase","stage"]):
        record = dict(phase=phase, stage=name, batches_observed=len(group), calls=group.calls.sum())
        for clock in ("inclusive_ms","exclusive_ms"):
            record.update({clock+"_"+k:v for k,v in stats(group[clock]).items() if k != "n"})
            record[clock+"_per_call_weighted_mean"] = group[clock].sum()/group.calls.sum()
        output.append(record)
    stage_table = pd.DataFrame(output).sort_values(["phase","exclusive_ms_total"],ascending=[True,False])
    stage_table.loc[stage_table.phase.eq("ALL")].to_csv(OUT/"timing_stages.csv",index=False)
    phases = ("WARMUP","EVALUATION","TAIL")
    scope = sorted(set(stages.stage) | {"operator_scope_graph","full_decode","repair_full_decode"})
    stage_grid = pd.MultiIndex.from_product([phases, scope],names=["phase","stage"])
    by_phase = stage_table.set_index(["phase","stage"]).reindex(stage_grid)
    for col in ("calls","batches_observed","inclusive_ms_total","exclusive_ms_total"):
        by_phase[col] = by_phase[col].fillna(0)
    by_phase.to_csv(OUT/"timing_stages_by_phase.csv")
    s = pd.DataFrame(sampling).set_index("batch")
    s["phase"] = b.cohort
    s.to_csv(OUT/"timing_sampling.csv")
    phase_output = []
    for phase,g in b.groupby("cohort"):
        sub = s.loc[s.phase.eq(phase)]
        elapsed = g.m4_wall_ms.sum()/1000
        phase_output.append(dict(phase=phase,batches=len(g),budget_exhausted=g.budget_exhausted.sum(),
            timed_out_using_s0=(g.budget_exhausted & g.used_fallback).sum(),
            candidates_started=g.candidates_started.sum(),candidates_completed=g.candidates_evaluated.sum(),
            completed_per_search_second=g.candidates_evaluated.sum()/elapsed,
            repairs_per_completed=g.repair_count.sum()/g.candidates_evaluated.sum(),
            mean_candidates_per_batch=g.candidates_evaluated.mean(),p50_candidates_per_batch=g.candidates_evaluated.median(),
            sampled_batches=len(sub),sampled_candidates=sub.sampled_candidates.sum(),
            candidate_cap_reached=sub.sampled_candidates.eq(8).sum(),candidate_cap_truncating=sub.candidate_samples_truncated.gt(0).sum(),
            candidates_truncated=sub.candidate_samples_truncated.sum(),detail_cap_reached=sub.detail_spans.eq(512).sum(),
            detail_cap_truncating=sub.detail_spans_truncated.gt(0).sum(),details_truncated=sub.detail_spans_truncated.sum()))
    pd.DataFrame(phase_output).to_csv(OUT/"timing_phase_work.csv",index=False)
    sampled = pd.DataFrame(sampled_requests)
    sampled.to_csv(OUT/"timing_requests.csv",index=False)
    outcomes = sampled.groupby(["outcome","request_cohort","final_run_state","budget_exhausted"]).agg(
        observations=("batch","size"),distinct_requests=("request_id","nunique")).reset_index()
    outcomes.to_csv(OUT/"timing_request_groups.csv",index=False)
    summary = {}
    for label, g in [("all",b),("profiled",b.loc[s.index]), *b.groupby("cohort"), *b.groupby("budget_exhausted")]:
        item = {}
        for col in ("batch_wall_ms","m1_wall_ms","m3_wall_ms","m4_wall_ms","m4_cpu_ms","m4_call_wall_ms",
                    "m4_wall_overrun_ms","validator_wall_ms","m5_wall_ms","type1","type2","type3","m4_tasks",
                    "graph_components","largest_component_tasks","evaluated_components","reused_components",
                    "candidates_started","candidates_evaluated","decode_count","repair_count","invalid_candidates"):
            item[col] = stats(g[col])
        item["repair_reasons"] = counter(g.repair_reasons)
        item["failure_reasons"] = counter(g.failure_reasons)
        item["component_size_counts"] = counter(g.component_size_counts)
        item["fallback_count"] = g.used_fallback.sum()
        item["retained_improved_count"] = (~g.used_fallback).sum()
        item["batches_accept_new"] = g.accepted_new.gt(0).sum()
        summary[str(label)] = item
    metadata = json.loads((RUN/"manifest.json").read_text(encoding="utf-8"))
    summary["file"] = metadata["profiling_counts"]
    summary["sampling"] = {c:dict(total=s[c].sum(),positive_batches=s[c].gt(0).sum()) for c in s.columns if c != "phase"}
    summary["request_counts"] = sampled.outcome.value_counts().to_dict()
    summary["first_conflict"] = sampled.first_conflict.fillna("none").value_counts().to_dict()
    summary["operators"] = {name:sum(r["operator_calls"][name] for r in p) for name in p[0]["operator_calls"]}
    summary["cache"] = {k:b.iloc[-1][k] for k in b.columns if k.startswith(("road_path_","road_distance_"))}
    (OUT/"timing_summary.json").write_text(json.dumps(summary,default=mapping,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print("Read",len(b),"batches;",len(p),"sampled batches;",len(sampled),"sampled request-batches;",len(stages),"stage-batches.")


if __name__ == "__main__":
    main()