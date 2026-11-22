"""Identify only S1 decodes whose repair evidence is unambiguous in saved counters."""
import json
from pathlib import Path
import pandas as pd

HERE = Path(__file__).resolve().parent
RUN = HERE.parents[1]/"outputs/20261121T151519Z-1ccd3331b2fa425e99cbfafde128809e"
batches = pd.read_csv(RUN/"batches.csv").set_index("batch")
rows = []
for line in (RUN/"profiling.jsonl").read_text(encoding="utf8").splitlines():
    r = json.loads(line)
    b = batches.loc[r["batch"]]
    stages = r["stages"]
    if not (b.repair_count or b.invalid_candidates or b.budget_exhausted):
        group, repairs = "no_conflicts_in_completed_batch", 0
    elif r["sampled_candidates"] == 1 and not r["detail_spans_truncated"] and "repair_remove" in stages:
        group, repairs = "isolated_S1_with_repairs", stages["repair_remove"]["calls"]
    else:
        continue
    assert stages["s1_decode"]["calls"] == 1
    rows.append(dict(batch=r["batch"],phase=b.cohort,group=group,repairs=repairs,
                     s1_decode_ms=stages["s1_decode"]["inclusive_ns"]/1e6,type1=b.type1,type2=b.type2,type3=b.type3))
frame = pd.DataFrame(rows)
frame.to_csv(HERE/"s1_comparison.csv",index=False)
for group, g in frame.groupby("group"):
    print(group,len(g),g[["s1_decode_ms","repairs"]].describe(percentiles=[.5,.95]).to_string())
