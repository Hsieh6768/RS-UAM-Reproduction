# 实验命令手册 — 用户手动启动指南

> P6 已实现单次 `simulate`、输入检查、逐批日志及运行内终态审计。当前待用户手动验证真实短窗口。
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

**E03 — 人工端到端实验**
```powershell
python -m src.cli.main simulate --config configs/micro.yaml --output-dir outputs/micro
```
运行内置两节点有向网络、固定班次和三条合成请求。可重复运行，每次都有独立结果目录。

**E04 — 正式配置与输入预检**

```powershell
python -m src.cli.main simulate --help
python -m src.cli.main simulate --config configs/debug_10min.yaml --fleet-size 50 --seed 101 --dry-run
```

预检读取 P0 小型清单、审计、样本表头及文件大小，显示参数、评价时长、预热时长与可用需求截止。实际运行会校验已准备输入的 SHA256，再扫描样本 ID、顺序及本次评价分母。E01 是历史制备入口，已有正式样本的实验从 E04 开始。

## 3. 真实数据短窗口验证（必须用户手动启动）

**E10 — 10分钟调试窗口，启用独立ScheduleValidator**
```powershell
python -m src.cli.main simulate --config configs/debug_10min.yaml --fleet-size 50 --variant rs_uam --seed 101
```
目标：真实曼哈顿路网、22:00预热衔接、评价10分钟窗口。**说明：若配置含两小时真实预热，则总模拟窗口长度大于10分钟。**

**E10 性能诊断（PROFILING，用户手动启动）**

在工程根目录的 PyCharm Terminal 执行：

```powershell
conda activate ridesharing
python -u -m src.cli.main simulate --config configs/debug_10min_profile.yaml --fleet-size 50 --variant rs_uam --seed 101
```

只检查配置时，在命令末尾添加 `--dry-run`。该配置继承 E10 的两小时预热、10 秒 M4 墙钟预算与最终独立校验。打开终端打印的新 run 目录，先看 `manifest.json` 中的 `profiling_counts`，再按 `batch` 关联 `profiling.jsonl` 与 `batches.csv`。采样开销会影响墙钟截止点，固定种子下仍可能出现不同的完成候选数与服务结果。采样参数及读数示例见 [P6 性能诊断实施报告](P6_PROFILING_IMPLEMENTATION_REPORT.md)。

**E11-60 — 60 分钟 SCC100 调试窗口及分层请求诊断**

E11-60 替代原 E11。评价时段为纽约当地 2013-05-05 00:00–01:00，延续两小时预热。全量评价请求有基础摘要，每个十分钟层固定种子选 30 个请求完整追踪至终态。预检返回目标、共享批次规划量、压缩空间估算与硬上限。真实运行由用户手动启动。

```powershell
conda activate ridesharing
python -B -m src.cli.main simulate --config configs/debug_60min_scc100.yaml --dry-run
python -B -u -m src.cli.main simulate --config configs/debug_60min_scc100.yaml --output-dir outputs/scc100/e11-60
```

查看运行目录内的 `request_diagnostics/audit.json`。请求诊断完整性为 `PASS` 时，全量批次和终态链均可读。输出格式、压缩限额及离线归因见 [E11-60 实施报告](P6_NET_E11_60_DIAGNOSTIC_IMPLEMENTATION.md)。

**历史 E11 — 30 分钟调试窗口**
```powershell
python -m src.cli.main simulate --config configs/debug_30min.yaml --fleet-size 50 --variant rs_uam --seed 101
```
保留历史配置和边界回归，供复核 00:30 起的 TAIL 竞争与排空。

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

## 7. 查阅本次结果

启动时终端显示本次绝对目录。默认结果位于 `outputs`，也可以用 `--output-dir outputs/debug10` 指定父目录。下面选取默认父目录中最新的一次运行。

```powershell
$run = Get-ChildItem outputs -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'manifest.json') } | Sort-Object Name -Descending | Select-Object -First 1
Get-Content (Join-Path $run.FullName 'manifest.json') -Raw -Encoding utf8
Get-Content (Join-Path $run.FullName 'console.log') -Tail 5 -Encoding utf8
Get-Content (Join-Path $run.FullName 'audit.json') -Raw -Encoding utf8
Get-Content (Join-Path $run.FullName 'summary.md') -Encoding utf8
```

先检查 manifest 状态。`COMPLETE` 应对应 `audit.json` 的 `PASS`；失败目录应查看 `failure.json`、`console.log` 和存在的 `errors/`。跨实验统计属于后续阶段。

## 8. 运行停止和输出

- 实验每批终端打印简洁摘要，完整文本同步`console.log`。
- 每次run在`outputs/<run_id>/`下生成 `summary.json, summary.md, requests.csv, batches.csv,
  vehicle_events.csv.gz, flight_usage.csv, performance.json, audit.json, manifest.json,
  config.yaml, console.log`。
- 正常结束状态`COMPLETE`；程序错误或最终accepted!=completed状态`FAILED`并非零退出；手动终止为`INTERRUPTED`。
- 同一个run目录不得被自动覆盖，不准将FAILED/INTERRUPTED样本纳入正式均值。

## 9. 参数覆盖

| 项目 | 入口与含义 |
|---|---|
| 车队与随机性 | `--fleet-size 50`、`--seed 101`。同一种子给出嵌套车辆位置序列，M4 使用独立的同种子生成器。P0 样本固定。 |
| 搜索预算 | 正式默认 `--budget-clock wall --budget-seconds 10`。30 秒配置使用 `--budget-seconds 30`。CPU 诊断示例为 `--budget-clock cpu --budget-seconds 10 --wall-safety-cap 30`。 |
| 独立校验 | `--validator` 或 `--no-validator`。10/30 分钟默认开，5h/48h 默认关。 |
| 再平衡 | `--no-m5`、`--m5` 或 `--variant noR`。noR 始终禁用新再平衡。 |
| 配置与时间 | YAML 通过相对路径 `extends` 继承；时间为纽约当地时间。复制短窗配置可改 `simulation.evaluation_end_exclusive`，延续从该时刻读取连续 P0 时间轴。 |
| 结果 | `--output-dir` 指定父目录；每次生成新子目录。完整字段及单位见 `docs/RUN_OUTPUTS.md`。 |

## P6-NET 独立实例

在已激活 `ridesharing` 的项目根目录手动运行。依次完成前一条并检查退出码 0 后再执行下一条。完整日志保存方式和失败处理见 [P6-NET 报告](P6_NET_SCC100_REPORT.md)。

```powershell
python -B -m src.cli.main prepare-network --config configs/p0_scc100.json
python -B -m src.cli.main prepare-data --config configs/p0_scc100.json
python -B -m src.cli.main audit-scc100 --config configs/debug_10min_scc100.yaml
```

有正式产物后，以下四条只读预检均可单独使用。

```powershell
python -B -m src.cli.main simulate --config configs/debug_10min_scc100.yaml --dry-run
python -B -m src.cli.main simulate --config configs/debug_30min_scc100.yaml --dry-run
python -B -m src.cli.main simulate --config configs/sensitivity_5h_scc100.yaml --dry-run
python -B -m src.cli.main simulate --config configs/paper_main_scc100.yaml --dry-run
```

数据 Gate 通过后，用户可单独启动新版真实 E10。输出进入指定父目录下的唯一运行目录。

```powershell
python -B -m src.cli.main simulate --config configs/debug_10min_scc100.yaml --output-dir outputs/scc100
```
