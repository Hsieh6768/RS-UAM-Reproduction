# 实验命令手册 — 用户手动启动指南

> E00–E02 包含可用的 P0 离线命令和轻量测试。仿真、正式运行审计和汇总仍是 P7 必须落实的接口契约。
> **真实数据的10/30分钟、五小时、48小时和批量消融均由用户亲自在Windows终端启动。**

## 0. 环境准备

在 PyCharm Terminal（PowerShell）中进入项目根目录并激活已存在的 `ridesharing` 环境。下面所有命令均以同一个终端的环境核验成功为前提。
```powershell
# 进入实际项目路径
Set-Location 'D:\Data\03_03_Pycharm\Projects_Pycharm\RS-UAM-Reproduction'

# 激活统一的 Conda 环境
conda activate ridesharing

# 检查环境
python -c "import sys; from pathlib import Path; print(sys.executable, sys.version); assert Path(sys.prefix).name == 'ridesharing' and sys.version_info[:3] == (3, 9, 25)"
conda info --envs
python -m pip check
```
依赖变更须先报告已装版本、兼容包、命令和风险并由用户确认。P0 使用 JSON 配置，正式 YAML 配置使用已批准的 PyYAML 6.0.3。

根目录已有 `new-york-190101.osm.pbf`、`nyc_2013_05_04_to_05_08.csv`、`air_routes_manhattan.json` 和用户后补的 `vertiports_manhattan.json`。归档命令保留根目录原件并复核 `data/raw/` 副本。

## 1. P0 离线清点和预处理

**E00 — 输入核验（不修改数据）**
```powershell
python -m src.cli.main validate-inputs --config configs/p0.json --dry-run
```
输出源文件表头、格式、字节数、哈希、PBF 头和航空站点/有向航线核验。道路方向、图结构和连通性在 E01 输出。

**E01 — 归档并生成路网与请求审计产物**
```powershell
python -m src.cli.main archive-inputs --config configs/p0.json
python -m src.cli.main prepare-network --config configs/p0.json
python -m src.cli.main prepare-data --config configs/p0.json
```
依次执行，任一步非零退出时先检查错误。结果在 `data/processed/p0_network/`、`data/processed/p0_requests/`，各含 SHA256 清单。已有产物会阻止重写，如需复核可为新配置指定新的 `processed_dir`，重新执行后两步。现有正式样本由请求 manifest 的 `official_samples` 锁定，以该清单的 CSV 路径和 SHA256 为准。

## 2. 快速自动测试（Codex可以运行）

**E02 — 全部轻量测试**
```powershell
python -m pytest -q tests/unit tests/oracle tests/integration
```
目标：微型路网/人工请求正确性、独立对照、状态/承诺规则，不能触发真实时间窗口仿真。

**E03 — 性能微基准（只在人工小图/固定快照，不运行完整真实数据时Codex可执行）**
```powershell
python -m pytest -q tests/performance
```
目标：短时函数级性能回归。只有在测试明确采用人工小图/短时限时可由 Codex 运行。

## 3. 真实数据短窗口验证（必须用户手动启动）

**E10 — 10分钟调试窗口，启用独立ScheduleValidator**
```powershell
python -m src.cli.main simulate --config configs/debug_10min.yaml --fleet-size 50 --variant rs_uam --seed 101
```
目标：真实曼哈顿路网、22:00预热衔接、评价10分钟窗口。**说明：若配置含两小时真实预热，则总模拟窗口长度大于10分钟。**

**E11 — 30分钟调试窗口**
```powershell
python -m src.cli.main simulate --config configs/debug_30min.yaml --fleet-size 50 --variant rs_uam --seed 101
```
目标：更长滚动集成验证、跨段因果/承诺/定时航班，逐批独立验证。

**E12 — CPU时间预算对照（诊断，不算论文同预算对照）**
```powershell
python -m src.cli.main simulate --config configs/cpu_budget_diagnostic.yaml --fleet-size 50 --variant rs_uam --seed 101
```
目标：与 E11 或同一时间窗Wall版同输入比较候选评价数、目标值及**真实墙钟耗时**。CPU版必须有额外Wall安全上限。

## 4. 论文五小时场景（必须用户手动启动）

**E20 — 5月5日 00:00–05:00；50车；RS-UAM**
```powershell
python -m src.cli.main simulate --config configs/sensitivity_5h.yaml --fleet-size 50 --variant rs_uam --seed 101
```
目标：论文§6.3敏感性窗口，预热需从5月4日22:00真实执行；末尾尾部竞争规则按相应配置延续。

## 5. 正式两天主实验（必须用户手动逐个启动）

**E30 — 50车，RS-UAM**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant rs_uam --seed 101
```

**E31 — 100车，RS-UAM**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 100 --variant rs_uam --seed 101
```

**E32 — 150车，RS-UAM**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 150 --variant rs_uam --seed 101
```

**E33 — 地面RS基线（与RS-UAM使用同场景输入）**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant rs --seed 101
```
其他车队规模仅需修改 `--fleet-size 100/150`；不得重新抽样需求。

## 6. 消融实验（必须用户手动逐个启动）

`noD`: 禁止任务图弱连通分量分解及相应分量级搜索；`noA`: 关闭动态算子选择；`noS`: 关闭SA接受较差候选；`OP`: 每请求仅最短备选方案；`noR`: 关闭M5。

**E40 — noD**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant noD --seed 101
```

**E41 — noA**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant noA --seed 101
```

**E42 — noS**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant noS --seed 101
```

**E43 — OP**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant OP --seed 101
```

**E44 — noR**
```powershell
python -m src.cli.main simulate --config configs/paper_main.yaml --fleet-size 50 --variant noR --seed 101
```

> 对上述每种变体，在用户确认正式批量实验时再扩展为50/100/150车、5次独立重复；Codex不能自动启动循环全部实验。

## 7. 汇总与审计（短脚本可手动运行）

**E50 — 审计单次运行的完整性**
```powershell
python -m src.cli.main audit-run --run-dir outputs/REPLACE_WITH_RUN_ID
```
目标：核对accepted==completed、请求唯一计数、M4及整批耗时统计、输入哈希与输出文件状态。

**E51 — 多实验汇总**
```powershell
python -m src.cli.main aggregate --runs-dir outputs --output-dir outputs/aggregate
```
目标：只汇总状态COMPLETE且通过审计的运行；分别出50/100/150与各变体的均值/标准差、论文参考对照。

## 8. 运行停止和输出

- 实验每批终端打印简洁摘要，完整文本同步`console.log`。
- 每次run在`outputs/<run_id>/`下生成 `summary.json, summary.md, requests.csv, batches.csv,
  vehicle_events.csv.gz, flight_usage.csv, performance.json, audit.json, manifest.json,
  config_resolved.yaml, console.log`。
- 正常结束状态`COMPLETE`；程序错误或最终accepted!=completed状态`FAILED`并非零退出；手动终止为`INTERRUPTED`。
- 同一个run目录不得被自动覆盖，不准将FAILED/INTERRUPTED样本纳入正式均值。

## 9. Codex 在 P7 的CLI验收

Codex必须验证以上命令的`--help`、配置解析、CLI参数覆盖与产物路径；不得为了使手册“看起来正确”而重写已批准的实验规则。
手册命令若因具体CLI设计必须变化，更新本文件及所有配置/文档并解释原因，确保**命令/实际执行入口一致**。
