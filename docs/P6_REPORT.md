# P6 全系统滚动调度集成报告

2026-11-21，Windows，`main`，基准 HEAD `3d637d6`。**P6 CODE_GATE PASS — READY_FOR_MANUAL_VALIDATION**。全部 291 项轻量测试通过，包含原有 235 项和新增 56 项。真实数据完整 P6 Gate 待用户手动运行短窗并审查结果。本阶段没有启动真实时间窗、批量实验或 P7，没有安装依赖或执行 Git commit/push。

## 1. 实施文件

| 文件 | 实际改动 |
|---|---|
| `src/simulation/rolling.py` | 新增滚动批次、动态时间归属、延续入流、逐段扩展班表、模块调用和计时。 |
| `src/simulation/transactions.py`、`src/simulation/rebalance.py` | 新增 M4 乘客联合提交与 M5 多车计划发布。 |
| `src/simulation/engine.py`、`src/simulation/events.py`、`src/simulation/service.py`、`src/simulation/connections.py` | 增加模拟世界身份、隔离暂存和一次发布、剩余任务编码、只读故障现场、可排空事件缓冲与道路用途；复用 P1 接受、时刻表、路径、容量及衔接校验。 |
| `src/m4/halns.py`、`src/m4/baseline.py` | 搜索结果关联实际快照、冻结的 M1 方案目录与 M3 资源行；逾期道路边诊断显示物理到达时刻和批次时刻。 |
| `src/m5/rebalancer.py` | 提案携带产生该提案的仿真实例身份，防止独立运行具有相同局部版本号时错用。 |
| `src/data/experiment.py`、`src/data/micro.py` | 新增 P0 正式清单、独立分母、顺序读取与延迟释放、嵌套车队初始化，以及人工输入。 |
| `src/cli/main.py`、`src/cli/experiment.py`、`configs/micro.yaml` | 接入 `simulate`，解析已有 YAML、命令行覆盖、只读预检、人工运行、单次运行生命周期和退出码。原有五份真实配置直接复用。 |
| `src/evaluation/outputs.py`、`src/evaluation/audit.py`、`src/evaluation/metrics.py` | 新增流式记录、失败现场、按唯一请求统计、逐事件审计与机器/人工报告。 |
| `tests/integration/test_p6_cli.py`、`test_p6_m4_commit.py`、`test_p6_rebalance.py`、`test_p6_rolling.py`、`test_p6_outputs.py` | 五个文件均位于 `tests/integration/`，共 56 项 P6 测试。 |
| `README.md`、`docs/EXPERIMENT_COMMANDS.md`、`docs/RUN_OUTPUTS.md`、`docs/P6_REPORT.md` | 更新入口、手动运行和输出字段说明，新增本报告。 |

## 2. 论文、批次与事件

PAPER §3.3 给出新到达、历史未满足、已接受且有剩余任务的三类请求。§4.1–4.2 式 (2)–(17) 描述完整服务、时窗、资源、衔接与容量。§5 Algorithm 1 规定 M1 生成方案、M3 搜索资源、M4 形成调度、M5 再平衡。§6.1–6.2、Table 4–5 提供仿真时间、车辆/班次参数及计时参照。

D001–D003、D008–D009 沿用 P1–P3 的 Plan、Leg、WalkLink、有向 RoadRouter 和稀疏候选。D004–D007 沿用 P4 的快照、物理解码、安全基础解和 HALNS。D010–D011 沿用 P5 整数运输提案与 P1 固定班次。D012–D015 决定样本时间归属、统计、计时、输出与运行权限。

每批按下列顺序推进：

1. 从预热起点计时，默认边界依次为 0、10、20 秒。注册最迟在本边界到达的释放信封，P1 按原始秒回放到达、道路、航班、步行、上下车和截止事件。请求负载在释放时才进入真实状态。边界事件先于本轮优化。道路保留精确累计耗时，在事件刻度应用 P1 取整规则。
2. 根据上一边界划分 Type1/2；Type3 保持已接受状态。普通等待请求在截止时刻仍有效，第一个严格超过截止的事件刻度执行过期。已接受请求漏接或无法按时终达会触发失败。
3. M1 为首次参与的有效请求生成方案，并保留等待请求的批间缓存。M3 读取当前车辆位置、航班及 Type3 锁定资源。M4 在冻结快照上用本批重新评分的 S0 和备选初解执行 HALNS，将最优合法解交给提交器。
4. M4 发布后取新快照，按有效未接受请求和可用空车调用 M5。输出实时摘要、批次行及本批已发生的事件。下一次决策的模拟时钟只由配置的周期决定。

## 3. 两次原子发布

**M4。** 单写者 P1 模拟器按世界 ID、快照版本、时刻、图版本及调度边界检查提交。搜索结果还须关联同一输入快照。暂存区复制可变请求/任务/航班表、车况与剩余道路队列、事件堆、序号、释放信封和当批历史；冻结记录及静态路网共享。新请求核对确切的 M1 Plan、全部正式资源及其 M3 资格；已有请求核对 Plan、资源和稳定 Task ID。随后验证全部未完成正式任务、跨车接续、实际预约和旅行时刻、车载人数、旧加新联合订座、固定航班、辅助步行及终达截止。

每辆车的路程从当前节点或当前边的精确终点接续。车辆任务版本使被替换的旧预约事件失效，当前物理道路边完成事件继续有效。所有同刻衍生事件先在暂存区执行。全部检查成功才一次替换真实模拟器的状态引用。最后一名乘客、最后一辆车或最后一班席位核验失败时，原有事件队列、车辆、乘客和席位仍可继续原轨迹。正式入口将此类错误视为 FAILED。

**跨批继承。** 同步发布的 SearchState 保存稳定未完成任务 ID、固定选择、算子评分和 RNG 状态；车辆未来动作顺序同时保存在真实车况。下一批过滤已执行任务，保留未执行后缀。B 优先的实际回放在首批发布 `BBAA`，B 在 0 秒上车，2 秒快照恢复 `BAA`，B/A 分别在 5/15 秒完成。测试还删除一项历史顺序，验证 `BaselineOrderRequired` 携现场抛出。预算耗尽时使用当批合法解，跨批继承字段不包含历史目标分数。

**M5。** 输入是 M4 发布后的快照。READY 逐项核验所有车辆身份、空闲状态、目标格可达入口、完整有向边序列、物理到达时间，以及跨格、留在本格、未使用三组车辆对供给的完整划分。网格整数流必须与覆盖和未覆盖需求一致。先对整份计划暂存，最后统一发布。SKIPPED 保持既有状态；SOLVER_FAILED 保存求解原因；UNEXECUTABLE 保存不能实现的流及路径诊断；两者均保留 M4 调度。RebalanceInvariantError 终止实验。已在再平衡的车辆可由 M4 合法接管，其在途道路边保持原物理到达时刻。

## 4. 航班、预热、评价与收尾

每个有向航线维护独立的下一班序号。P1 `AirNetwork.flight(route_id, index)` 使用相对于预热起点的固定相位，正式配置相位 0、班距 180 秒、容量 4。滚动生成覆盖当前时刻和所有已释放待选请求的合法时间范围；同一班次只注册一对起降事件。先执行已到时起飞事件，再搜索新资源。共享乘客各自验证就绪和订座；降落后才执行后段任务。历史订座保留。空班看前延伸可能超出最后执行时刻，报告记录其真实状态。

正式需求沿 P0 清单内 WARMUP、EVALUATION、TAIL 三份样本的原始时间轴读取。真实运行启动时核验输入身份、顺序、唯一 ID 和本次窗口独立评价分母。P0 固定数量为 2,490 / 33,565 / 18,055，道路 18,713 节点、26,461 有向边，航空 10 站、60 有向航线。车位由稳定节点表有放回均匀抽取，相同种子的不同车队共享初始位置前缀。

主窗口从 5 月 4 日 22:00 开始连续调度；5 月 5 日 00:00 只改变新到请求的统计归属。评价按原始时间落在半开区间内判定。短窗 00:10 之后的同日样本立即作为该次运行的 TAIL，新需求继续公平竞争。在评价结束后，按实际事件时间检查最后一个评价请求是否终结；在此前到达的尾部需求仍可参与调度，评价请求在结束后合法接受仍计成功。关闭入流后完成已有承诺、让尚未接受的等待请求按原截止过期、走完已发布再平衡。源覆盖到期仍有评价请求未决会报错。终态关闭同时核对所有请求、车辆和已订航班，任一计数失配都不能产出 COMPLETE。

## 5. CLI、开关和证据文件

正式运行命令统一维护在 [实验命令手册](EXPERIMENT_COMMANDS.md)。实际入口为 `python -m src.cli.main simulate`。10/30 分钟配置对每批最终最佳方案调用一次 P4-A 独立 ScheduleValidator。5 小时和两天默认关闭此项；P1 运行时检查和整批提交检查始终执行。默认预算为 M4 自身 10 秒墙钟，可切换 30 秒。短窗 CPU 诊断必须另给墙钟安全上限。车队、种子、变体、M5 和输出父目录可覆盖。

每次运行创建独立 run 目录。主要文件为 `manifest.json`、`config.yaml`、`console.log`、`batches.csv`、`requests.csv`、`vehicle_events.csv.gz`、`flight_usage.csv`、`performance.json`、`audit.json`、`summary.json`、`summary.md`。事件按批写入 gzip 并排空内存缓冲；CSV 提供每批的性能、请求计数、真实时间及资源明细。异常目录含 `failure.json` 的失败阶段、出错批次、现场、栈、最后成功批次，以及模块产生的专项诊断。已开始的实验以非零码退出，失败时移除正常总结。字段、状态和单位见 [输出格式说明](RUN_OUTPUTS.md)。

**指标。** SR 以评价唯一合法接受数除以本次评价有效请求总数。审计要求 accepted_evaluation 等于 completed_evaluation，其他已接受请求也须闭合。按原始提交秒至最终送达秒计完整出行；绕行减去 P1 静态有向道路直达时间再除 60，保留负值。模式为 SHRS、MHRS、RS-UAM、Unserved。审计独立重读事件，核对乘客任务前驱、车辆起终位置、在车人数及容量、航班实际起降和订座；M5 调车数独立于 SR。

**性能。** 记录 M1 及缓存命中，M3 及索引构建，M4 搜索 Wall/CPU、含终检的函数调用 Wall/CPU、独立校验耗时，M5 的 Wall/CPU 及求解、配车开销，以及完整调度批次至输出摘要前的墙钟。分别汇总平均、中位、p95、p99、最大值及超时率。记录搜索、修复、解码、算子、分量、道路缓存、事件队列和状态表大小。按 D014，整批可超过参考限时，即使 M4 自身在预算内。

## 6. 实测与 Gate

环境为 Conda `ridesharing`、`D:\Programs_D\Anaconda\envs\ridesharing\python.exe`、Python 3.9.25。核实 pytest 8.4.2、NumPy 2.0.2、pandas 2.3.3、NetworkX 3.2.1、SciPy 1.13.1、Rtree 1.4.1、Gurobi 12.0.3、PyYAML 6.0.3。Windows 沙箱内首次历史回归的 9 个临时目录夹具遇到拒绝访问；使用同一解释器和可写临时目录重新运行通过。下表为实际复验。

| 命令（在该环境中） | 结果 |
|---|---|
| `python -B -m pytest -p no:cacheprovider -q tests/unit tests/oracle tests/integration --ignore-glob="*p6*"` | 原有 235 passed，0 failed |
| `python -B -m pytest -p no:cacheprovider -q -k p6` | 56 passed，235 deselected，5.79 s |
| `python -B -m pytest -p no:cacheprovider -q` | **291 passed，0 failed，9.28 s** |
| `simulate --help`；五份真实配置逐一 `--dry-run` | 退出码 0。预热均为 7,200 s；评价时长分别为 600、1,800、18,000、172,800 s；另有 1,800 s CPU 诊断。输入覆盖末端为 2013-05-08 00:00:00，不含该刻。 |
| `python -B -m src.cli.main simulate --config configs/micro.yaml --output-dir outputs/p6_code_gate` | 退出码 0，7 个批次、3 条请求、6 项乘客任务、17 条事件；独立回读清单列出的 10 个产物 SHA256 全部相符。 |

专项按用户 A–E 要求覆盖批间与恰好边界到达、真实秒、过期等号、M4 耗时与模拟钟分离、共享车辆/航班、多车联合失败及旧世界回放、跨批 BBAA、M5 READY/SKIPPED/SOLVER_FAILED/UNEXECUTABLE、当前边改派、两小时边界、评价后接受、动态尾部、用尽数据、独立审计故障、校验开关、种子重复性与命令行失败退出。含真实经过 M1→M3→M4→整批提交→M5→P1 事件→统计的人工 CLI 运行。另一个小图中，M4 首批接受 A，M5 将唯一剩余空车跨格调动；B 在 20 秒被接受、37 秒完成。地空小图验证两名乘客共用 180 秒起飞、185 秒降落的班次。步行组合用例从 160 秒接受，步行至 168 秒，180 秒登机、185 秒降落，尾随步行后 193 秒完成。

[人工运行报告](../outputs/p6_code_gate/20261121T091414Z-45ac94cba4b74b2ea92f881bda5e1ae2/summary.md) 的预热/评价/尾部各一条，均已完成。评价请求 13 秒提交、20 秒接受、37 秒结束，SR 为 1/1，绕行为 7/60 min。该次完整批次均值 5.4541 ms，M4 搜索均值 4.2380 ms。实际日志节选：

```text
[2013-05-05 00:00:20] batch=4 TAIL new=1 pending=0 accepted=1 expired=0 in_service=2 fleet(idle/loaded/pickup/rebalance)=1/1/0/0 M4=11.8ms cpu=15.6ms evals=101 budget_exhausted=False M5=SKIPPED rebalanced=0 total=13.2ms EVAL(accepted/completed)=1/0
```

## 7. 真实验证待办与风险

- 真实 10/30 分钟及长窗口运行次数均为 0。真实服务率、闭合、吞吐和尾部长度仍待验证。论文 Table 5、作者未公开的初始车位/输入筛选、固定拓扑差异不作验收阈值。
- 既有 M1 首次规划、M3 建索引、M5 Gurobi 启动与实际道路选车都可能增加端到端延迟。调度器会保留完整请求、任务、航班历史。暂存和快照按表复制，M3/M4 及每批统计仍会访问增长中的历史表。事件虽已流式写入，48 小时内存和实际整体运行时间仍须实测。内存字段仅给出所标容器的浅层大小。
- 固定种子的人工完成结果可复现。真实墙钟截止具有负载敏感性，Windows 短样本 CPU 读数可为 0。完整验收需审阅真实运行的 manifest、audit、summary、performance，以及事件和批次记录。
- 申请用户在已激活 `ridesharing` 的 PyCharm Terminal、仓库根目录手动执行：

```powershell
python -m src.cli.main simulate --config configs/debug_10min.yaml --fleet-size 50 --variant rs_uam --seed 101
```

该命令含两小时预热、10 分钟半开评价窗口及按状态结束的延续期。当前停在 P6 代码门槛。完整 P6 Gate 待真实短窗结果审查，P7 尚未启动。

