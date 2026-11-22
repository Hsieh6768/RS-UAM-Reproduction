"""Render the two figures from saved audit tables, without simulator imports."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(__file__).resolve().parent


def main():
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    positions = pd.read_csv(HERE/"scc_positions.csv", usecols=["run","time","source_scc"])
    positions["in_core"] = positions.source_scc.eq(0)
    counts = positions.groupby(["run","time"]).in_core.sum()
    fig, ax = plt.subplots(figsize=(8.8,3.3), layout="constrained")
    ax.axvspan(120,130,color="#f5ddab",alpha=.65,label="Evaluation")
    for label, style, color in [("profiling","-","#127381"),("baseline","--","#864780")]:
        s = counts.loc[label]
        ax.step(s.index/60,s,where="post",label=label.capitalize(),ls=style,c=color,lw=2)
    ax.set(xlabel="Simulated minutes since 22:00", ylabel="Vehicles in largest SCC", yticks=[43,44,45,46],
           ylim=(42.7,46.7),title="Fleet in the largest SCC at each completed batch")
    ax.grid(axis="y",alpha=.25)
    ax.legend(loc="upper right",ncols=3,frameon=False)
    fig.savefig(HERE/"fleet_core.png",dpi=165)
    plt.close(fig)

    data=pd.read_csv(HERE/"timing_stages_by_phase.csv")
    stages=["repair_local_physical","repair_graph","repair_remove","select_conflict"]
    data=data.loc[data.stage.isin(stages) & data.phase.ne("ALL")].pivot(index="stage",columns="phase",values="exclusive_ms_total")
    fig, ax=plt.subplots(figsize=(8.8,3.5),layout="constrained")
    data.reindex(stages)[["WARMUP","EVALUATION","TAIL"]].div(1000).plot.barh(
        stacked=True,ax=ax,color=["#236875","#dc9743","#7ba8a7"],width=.65)
    ax.invert_yaxis()
    ax.set(xlabel="Recorded exclusive time (s)",ylabel="",title="Repair work captured by the bounded sampler")
    ax.legend(frameon=False,title=None)
    fig.savefig(HERE/"sampled_repairs.png",dpi=165)
    plt.close(fig)


if __name__=="__main__":
    main()
