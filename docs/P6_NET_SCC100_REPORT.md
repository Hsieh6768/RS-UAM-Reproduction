# P6-NET · Manhattan 最大强连通分量与 100 m 吸附

2026-11-22。开发分支 `feat/p6-net-scc100`，起点为 `a6a8691`，与 `main` 及标签 `p6-pre-scc100-20261122` 对应的提交一致。

**代码 Gate 已通过。数据 Gate 等待用户手动生成正式数据。** 本轮仅使用人工输入执行预处理及仿真。正式 SCC100 的节点、边、总体、样本数及 SHA256 均须从新产物核验。历史 E10 和 Profiling 结果保留在原目录。

## 新旧实例与方法依据

| 项目 | 历史 D011 | P6-NET D016 |
|---|---|---|
| 道路 | 完整 Manhattan 有向图，`data/processed/p0_network/` | 最大 SCC 诱导有向多重图，`data/processed/p0_scc100/network/` |
| 请求 | 在完整图映射并检查方向可达性，`data/processed/p0_requests/` | 全部基础合格请求重新吸附，双端 ≤100 m，`data/processed/p0_scc100/requests/` |
| 离线配置 | `configs/p0.json` | `configs/p0_scc100.json` 继承 P0 |
| E10 入口 | `configs/debug_10min.yaml` | `configs/debug_10min_scc100.yaml` |
| 结果身份 | 原 E10 清单及其输入 SHA256 | 运行清单标记 `dataset_variant=scc100`、策略、阈值、输入数量和 SHA256 |

论文 §6.1–6.2、Tables 4–5 提供案例和实验参数背景。D016 是用户批准的数据处理决策。§3.2 与 D001–D002 的直达道路基准和时窗、§5 Algorithm 1 的调度顺序、D003–D007 的 M4 解码与搜索、D010 对应 §5.4 式 (25)–(31) 的再平衡模型均沿用。D011 管理固定航空拓扑和地图匹配，D012–D013 管理抽样、预热、尾部和指标，D014–D015 管理验证、预算和运行权限。

## 生成与审计链

1. 两个生成命令分别复核四份归档输入的大小、SHA256 和配置来源。道路阶段用同一 PBF 的 Manhattan 关系 8398124、P0 车行 way 筛选、单行方向、原始 OSM ID 和速度构造全图。按 SCC 节点数降序、最小节点 ID 升序确定分量，保留所有内部有向边及其 way、长度、时间和平行边。道路审计保存全图与新图的实际计数。新图自身只有一个强连通分量即证明其全部节点对有有向路径。
2. 10 个起降场同时保留全图映射、新映射及两端各自的吸附误差，`road_node` 是 SCC100 加载器使用的站点道路节点。60 条有向航线及 180 s 班距仍来自原航空输入。旧 P0 站点审计记录的全图与最大 SCC 映射有 0 个站点改变，最大误差为 107.285 m。这是历史证据，正式新文件须再验。乘客 100 m 筛选不适用于站点。
3. 需求阶段扫描原始归档 TLC 的所有行，以从 1 开始的原始记录序号为请求 ID。沿用 P0 的当地时刻、区域、人数、原始 OD ≥500 m 和历史异常标记。对所有基础合格行在新图节点上以球面三维坐标做最近节点查询，按原半径 6,371,009 m 计算弧长。任一端 >100 m 的行计一次主要排除，同时记录 O、D 及双端超限数。合格同点映射将触发错误。两端若吸附到同一点且各 ≤100 m，原始 OD 由三角不等式至多 200 m，与基础过滤矛盾。
4. `mapped_population.csv.gz` 保存全部基础合格行、原始字段、两端节点及误差、原始与映射 OD 距离和资格。 `tlc_audit.json` 从原始行数按互斥原因逐项扣减，保留重叠标记和三期计数。仅对合格总体调用原 `stratified_sample()`。每个 cohort 采用 5% 正中取偶配额，小时余数相同则先分给较早小时，按原 ID 排序后用种子 20130505 无放回抽取。保留原接客时间列；新版释放时间列亦保存该时间戳的原始文本。
5. `sampling_by_hour.csv` 及 `warmup_scc100.csv`、`evaluation_scc100.csv`、`tail_scc100.csv` 随需求清单一起发布。两级清单保存有效 P0 配置、子配置和父配置指纹、四个原始 SHA256、环境、Git 提交和当前源码树哈希；目录内产物逐文件计算大小与 SHA256。需求清单再绑定道路清单、实际道路文件和三个样本。网络阶段的需求数量在第二阶段的需求清单中闭合。两个目录分别在同级临时目录构建、校验完成后发布；已有目标会拒绝覆盖。
6. P6 先核对所选变体、路径、清单链、来源、图与样本指纹、计数和站点，再从该图构建路由器。SCC100 预检还读实际图和三期样本，核对请求 ID 唯一、节点归属、双端阈值和原时刻。M1 缓存由图指纹和航空映射区分，M3 读取当前车辆位置，M4 的直达基准在请求释放时由当前路由器计算。按种子从该路由器节点表生成车队；M5 从同一路由器的道路入口构造 1000 m 网格。终态审计验证本次真实提交和事件链。
7. 手动 `audit-scc100` 再校验原件和归档、两个实例的产物和指纹。SciPy 稀疏图 SCC 独立对照 P0 的 NetworkX，逐节点比较 ID/坐标、逐边比较方向、重数和权重。对每个基础合格 ID 比较原 TLC 的全部八列，以及历史 P0 在**抽样前**已单独保存的最大 SCC 映射。另以穷举 haversine 对每第 4096 行的两个端点（含首行）和全部站点独立核对最近节点。工具核对全部样本内容、逐层人数、配额、种子抽出的 ID 和完整账本，最后调用新版 E10 的只读入口。参考数量有差异时输出实际值和具体差项并非零退出。

## 手动 Gate B

在项目根目录的 PyCharm PowerShell Terminal 中执行。下面的脚本为本次手动准备创建独立日志目录。各步成功后才执行下一步。若网络已成功发布而后续步骤中断，可从 `prepare-data` 或 `audit-scc100` 单独继续；已存在的完整目录应保留供核查。

```powershell
conda activate ridesharing
python -c "import sys; print(sys.executable, sys.version)"
$netLog = Join-Path 'outputs' ('p6_net_prepare_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $netLog | Out-Null
python -B -m src.cli.main prepare-network --config configs/p0_scc100.json 2>&1 | Tee-Object -FilePath "$netLog/network.log"
if ($LASTEXITCODE -ne 0) { throw 'prepare-network failed' }
python -B -m src.cli.main prepare-data --config configs/p0_scc100.json 2>&1 | Tee-Object -FilePath "$netLog/requests.log"
if ($LASTEXITCODE -ne 0) { throw 'prepare-data failed' }
python -B -m src.cli.main audit-scc100 --config configs/debug_10min_scc100.yaml 2>&1 | Tee-Object -FilePath "$netLog/data_gate.log"
if ($LASTEXITCODE -ne 0) { throw 'DATA_GATE FAIL — review data_gate.log' }
```

数据 Gate 的参照值如下。验收以生成文件和原始记录的独立核对为前提。

| 核验项 | 用户提供的参考值 | 本轮正式实测 |
|---|---:|---|
| 最大 SCC 节点 / 有向边 | 16,808 / 24,491 | 待生成 |
| 基础合格 / 吸附超限 / 最终合格 | 1,084,915 / 24,950 / 1,059,965 | 待生成 |
| WARMUP / EVALUATION / TAIL | 2,440 / 32,882 / 17,677 | 待生成 |
| 三期样本合计 | 52,999 | 待生成 |

新 E10 保留 50 车、RS-UAM、车队/HALNS 种子 101、10 s 调度批次、M4 10 s 墙钟预算、逐批 ScheduleValidator、2 h 预热、10 min 评价及原尾部规则。30 min 也启用逐批验证；5 h 和 48 h 模板沿用正式模式设置。所有模板均可在生成数据后以 `simulate --config <配置文件> --dry-run` 只读检查。每个配置的完整命令见 [实验命令手册](EXPERIMENT_COMMANDS.md)。

仅在正式数据验收通过后，由用户单独启动 E10。

```powershell
python -B -m src.cli.main simulate --config configs/debug_10min_scc100.yaml --output-dir outputs/scc100
```

## Gate A 证据

环境为 Conda `ridesharing`，解释器 `D:/Programs_D/Anaconda/envs/ridesharing/python.exe`，Python 3.9.25，NumPy 2.0.2、SciPy 1.13.1、pytest 8.4.2、Gurobi 12.0.3。pytest 默认临时目录在 Windows 沙箱中首次返回拒绝访问，后续使用同一解释器和可访问的系统临时目录。

- 人工预处理只替换 PBF 对象读取与行政面提取，真实执行车行图构建、裁剪、映射、清洗、抽样、写盘和公开 CLI 加载。11 节点/20 边得到 10 节点/19 边，保留平行边。人工图全部 100 个有序节点对逐边可达。66 条原始记录中 3 条原始 OD 过短、3 条单端超限，60 条合格，三期各抽 1 条。原全图的 3 个不可达 OD 在重新吸附后进入新总体。
- 50 个初始车位及全部网格入口均在新图，人工站点由已删除的 11 映射到 10，误差超过 100 m 仍可接驳。可同时持有旧图与新图的独立路由器。公开仿真入口在人工短窗完成评价乘客、M4 提交及终态审计，输出 `COMPLETE`。
- 错配测试覆盖旧图路径、错误 manifest、变体缺失、跨期样本、等长篡改时间或航空文件、错误原始来源，以及重算了 SHA256 但引用已删除节点的样本。人工数据通过核账后，对正式参考值有差异的审计 CLI 返回 `DATA_GATE FAIL`、实际数量、E10 预检及非零退出码。此用例仅替代了历史 NYC 规模检查所需的四个来源身份。
- 旧正式 E10 的限时只读预检得到 `DRY_RUN full 18713 26461` 和 2,490 / 33,565 / 18,055，车队及 HALNS 种子均为 101。新正式目录缺失时同一预检明确指出 `p0_scc100/requests/manifest.json` 并退出 1，未创建运行目录。
- 验证命令包括 `python -B -m pytest -p no:cacheprovider -q tests/integration/test_p6_net.py`、`python -B -m pytest -p no:cacheprovider -q tests/oracle/test_scc100_oracles.py`、全量 `python -B -m pytest -p no:cacheprovider -q` 和 `python -B -m src.cli.main audit-scc100 --help`。开发中先观测到缺少配置、来源错配、同字节数的语义错配及单节点 SCC 零边比例失败，再修正并运行全量回归。

全量结果为 `328 passed in 16.73s`，0 失败。共 309 项既有轻量测试和 19 项本轮测试。

**P6-NET CODE_GATE PASS — READY_FOR_MANUAL_DATA_PREPARATION**

## 影响与局限

100 m 球面距离只刻画 GPS 到道路节点的几何偏差。桥隧、高架、临水道路的上下层或步行入口仍可能匹配异常，可由原始坐标、节点 ID 和误差复核。OSM 快照、Manhattan 边界以及批准的固定航空拓扑与论文实例之间原有的差异仍在。地图匹配耗时不进入乘客行程或容忍窗。

新旧总体、抽中 ID、直达时间、网格可用入口及同一种子对应的实际车位都可能不同。旧 E10 与新 E10 可报告各自分母和结果；如要归因于道路裁剪，还需控制需求和起始位置。代码 Gate 无法推断服务率或 M4/M5 性能变化。全量重读 TLC、保存压缩空间账本及数据 Gate 的逐行回放均有时间与内存开销，本轮没有测量正式耗时。

## 修改文件和工作区

下方为开发分支上最终的 `git status --short`。其中 `analysis/p6_real10/`、`data/`、`handoff_manifest.json`、`outputs/`、`vertiports_manhattan.json` 在开始时已未跟踪。历史交付清单、P6 报告及输入清单说明了这些路径的来源。其余行是本轮修改文件或新增文件，均待用户审查提交。

```text
 M README.md
 M docs/ARCHITECTURE.md
 M docs/DATA_AND_PROTOCOL.md
 M docs/DECISIONS.md
 M docs/EXPERIMENT_COMMANDS.md
 M docs/PAPER_TRACEABILITY.md
 M docs/RUN_OUTPUTS.md
 M docs/TEST_AND_GATES.md
 M src/cli/experiment.py
 M src/cli/main.py
 M src/data/experiment.py
 M src/data/p0.py
 M src/data/sources.py
 M src/evaluation/outputs.py
 M src/network/flights.py
 M src/network/prepared.py
 M src/network/road.py
?? analysis/p6_real10/
?? configs/debug_10min_scc100.yaml
?? configs/debug_30min_scc100.yaml
?? configs/p0_scc100.json
?? configs/paper_main_scc100.yaml
?? configs/sensitivity_5h_scc100.yaml
?? data/
?? docs/P6_NET_SCC100_REPORT.md
?? handoff_manifest.json
?? outputs/
?? src/data/instance.py
?? src/data/scc100.py
?? src/data/scc100_audit.py
?? tests/integration/test_p6_net.py
?? tests/oracle/test_scc100_oracles.py
?? vertiports_manhattan.json
```

