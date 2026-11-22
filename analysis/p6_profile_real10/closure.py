"""Bounded, saved-artifact-only comparison. Reuse P6's independent E10 reader."""
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "p6_real10"))
import complete_audit as forensic
from run_audit import digest, verify_console

RUNS = {"B": "20261121T095345Z-f4ee53eccb874218bbe3fa3ced6fcb2f",
        "P": "20261121T151519Z-1ccd3331b2fa425e99cbfafde128809e"}


def check_run(run):
    # Only the reader functions are reused. Its historical writing main is never called.
    forensic.RUN = run
    m, s, a, perf = [forensic.read_json(run / name) for name in
                     ("manifest.json", "summary.json", "audit.json", "performance.json")]
    assert m["status"] == s["status"] == "COMPLETE" and a["status"] == "PASS"
    assert set(m["output_checksums"]) == {p.name for p in run.iterdir()} - {"manifest.json"}
    for name, info in m["output_checksums"].items():
        assert digest(run / name) == info["sha256"] and (run/name).stat().st_size == info["size_bytes"]
    assert digest(run/"config.yaml") == m["configs_sha256"]
    for phase, path in zip(("WARMUP","EVALUATION","TAIL"), m["sample_paths"]):
        assert digest(ROOT/path) == m["processed_sample_sha256"][phase]
    req, cohorts, cutoff = forensic.request_data(m)
    batches = forensic.read_csv(run/"batches.csv", 1100)
    verify_console(batches, (run/"console.log").read_text(encoding="utf8"), interrupted=False)
    ticks, resources = forensic.event_audit(m, req, int(batches[-1]["batch_time"]))
    assert resources["events"] == a["event_counts"] and resources["tasks"] == a["task_count"]
    assert resources["seats"] == a["flight_seats_reserved"] and resources["road_meters"] == a["road_metres"]
    ev = cohorts["EVALUATION"]
    assert ev["valid"] == s["evaluation_requests"] == a["evaluation_count"] == m["expected_evaluation_requests"]
    assert ev["service_rate"] == s["service_rate"] and ev["expired"] == s["expired_evaluation"]
    for key in ("accepted","completed"):
        assert ev[key] == s[key+"_evaluation"] == a[key+"_evaluation"]
    assert cohorts["ALL"]["valid"] == a["request_count"]
    assert cohorts["ALL"]["completed"] == a["accepted"] and cohorts["ALL"]["expired"] == a["expired"]
    assert s["cohorts"] == {k:v["valid"] for k,v in cohorts.items() if k != "ALL"}
    for key, ref in (("mean_trip_min","mean_total_trip_min"),("mean_wait_min","mean_wait_min"),("mean_detour_min","mean_detour_min")):
        assert abs(ev[key]-s[ref]) < 1e-8
    assert all(ev["modes"][k] == v for k,v in s["service_modes"].items())
    assert abs(resources["seat_utilization"]-s["flights"]["seat_utilization"]) < 1e-12
    changes = defaultdict(Counter)
    for r in req.values():
        for label, tick in (("arr", r["orig"]), ("acc", r["accept_time"]), ("done", r["actual_final_dropoff"]),
                            ("exp", None if r["accept_time"] is not None else r["orig"]+421)):
            if tick is not None:
                d = changes[(tick+9)//10]
                d[label] += 1
                if r["cohort"]=="EVALUATION":
                    d["eval_"+label] += 1
    total = Counter()
    for i,b in enumerate(batches):
        t = i*10
        d = changes[i]
        total.update(d)
        assert (i+1,t,forensic.cohort(t)) == (int(b["batch"]),int(b["batch_time"]),b["cohort"])
        assert [d[k] for k in ("arr","acc","exp")] == [int(b[k]) for k in ("new_req","accepted_new","expired")]
        assert total["arr"]-total["acc"]-total["exp"] == int(b["pending_after"])
        assert total["acc"]-total["done"] == int(b["accepted_incomplete"])
        assert [total[k] for k in ("eval_acc","eval_done")] == [int(b[k]) for k in ("evaluation_accepted","evaluation_completed")]
        assert sum(int(b[k]) for k in ("fleet_idle","fleet_pickup","fleet_loaded","fleet_rebalancing")) == 50
        assert sum(ticks[x] for x in range(t-9,t+1)) == int(b["batch_event_count"])
    assert [batches[-1][k] for k in ("pending_after","accepted_incomplete","fleet_idle")] == ["0","0","50"]
    assert len(batches)==s["batch_count"]==perf["batch_count"]
    totals = period(batches)
    for key in ("budget_exhausted","batch_wall_budget_exceeded"):
        assert totals[key] == perf["flags_count"][key]
    assert totals["m5_status"] == perf["m5_status_counts"]
    for k in ("repair_count","candidates_evaluated","candidates_started","batch_wall_ms","m4_wall_ms"):
        assert np.isclose(sum(float(b[k]) for b in batches),perf[k]["total"],rtol=1e-10)
    return m, req, batches, dict(cohorts=cohorts,resource_audit=resources,outputs_verified=len(m["output_checksums"]),
        eval_terminal_second=cutoff, last=batches[-1]["sim_datetime"],status=m["status"],audit=a["status"],
        run_wall_s=(datetime.fromisoformat(m["end_wall"])-datetime.fromisoformat(m["start_wall"])).total_seconds(),
        periods={ph:period([b for b in batches if b["cohort"]==ph]) for ph in ("WARMUP","EVALUATION","TAIL")},all=totals)


def period(bs):
    def nums(k):
        return np.array([float(b[k]) for b in bs])
    result = dict(count=len(bs),budget_exhausted=sum(b["budget_exhausted"]=="True" for b in bs),
                  batch_wall_budget_exceeded=int(sum(nums("batch_wall_ms")>10000)),
                  m5_status=Counter(b["m5_status"] for b in bs),fallback=Counter(b["used_fallback"] for b in bs),
                  rebalanced=int(sum(nums("rebalanced"))),eval_accepted_end=int(bs[-1]["evaluation_accepted"]),
                  eval_completed_end=int(bs[-1]["evaluation_completed"]))
    for k in ("m4_wall_ms","batch_wall_ms","m4_cpu_ms","m1_wall_ms","m3_wall_ms","validator_wall_ms","m5_wall_ms","candidates_evaluated"):
        x=nums(k)
        result[k]=dict(total=float(x.sum()),mean=float(x.mean()),p50=float(np.median(x)),p95=float(np.percentile(x,95)),
                       p99=float(np.percentile(x,99)),maximum=float(x.max()))
    result["throughput_per_s"]=nums("candidates_evaluated").sum()*1000/nums("m4_wall_ms").sum()
    return result


def main():
    assert sys.version_info[:3]==(3,9,25) and Path(sys.prefix).name=="ridesharing"
    manifests, sets, rows, results, configs = {}, {}, {}, {}, {}
    for label, folder in RUNS.items():
        run = ROOT/"outputs"/folder
        manifests[label], req, rows[label], results[label] = check_run(run)
        sets[label] = {rid: r for rid,r in req.items() if r["cohort"]=="EVALUATION"}
        configs[label] = yaml.safe_load((run/"config.yaml").read_text(encoding="utf8"))
    profile = configs["P"].pop("profiling")
    assert configs["P"]==configs["B"] and sets["B"].keys()==sets["P"].keys()
    matches = {k:manifests["B"][k]==manifests["P"][k] for k in manifests["B"] if k in manifests["P"]}
    for k in ("initial_fleet_nodes","processed_sample_sha256","road_graph_version","code_commit","seed_halns",
              "seed_fleet","input_manifest_sha256","road_network_sha256","air_routes_sha256","vertiports_sha256"):
        assert matches[k]
    ids=sorted(sets["P"])
    with (HERE/"evaluation_pairs.csv").open("w", encoding="utf8", newline="") as f:
        w=csv.writer(f)
        w.writerow(("request_id","original_second","B_state","P_state","B_mode","P_mode"))
        for rid in ids:
            b,p = sets["B"][rid],sets["P"][rid]
            assert all(b[k]==p[k] for k in ("orig","mapped_origin","mapped_dest","party_size"))
            w.writerow([rid,b["orig"],b["state"],p["state"],b["service_mode"],p["service_mode"]])
    results["paired_eval"]=Counter("/".join(sets[label][rid]["state"] for label in RUNS) for rid in ids)
    results["first_different_batch"]={k:next((p["batch"] for b,p in zip(rows["B"],rows["P"]) if b[k]!=p[k]),None)
                                      for k in ("candidates_evaluated","best_score","accepted_new","accepted_incomplete","m5_status")}
    tree=hashlib.sha256()
    for path in sorted((ROOT/"src").rglob("*.py")):
        tree.update(path.relative_to(ROOT).as_posix().encode()+b"\0"+bytes.fromhex(digest(path)))
    results["identity"]=dict(manifest_fields_equal=matches,profile_settings=profile,other_effective_config_equal=True,
                             current_source_sha256=tree.hexdigest(),
                             source={k:m["source_tree_sha256"] for k,m in manifests.items()})
    (HERE/"closure.json").write_text(json.dumps(results,indent=2)+"\n",encoding="utf8")


if __name__=="__main__":
    main()
