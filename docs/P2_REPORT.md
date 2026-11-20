# P2 验收报告 · M1 反向 Pulse 与 Top-K

2026-11-20，Windows，分支 `main`。**P2 Gate PASS。** P2 的 32 项轻量测试及原有 73 项 P0/P1 回归全部通过。Pulse 与独立穷举在 10 组条件、27 次结果对照中一致。正式请求 554581 返回两条完整标准 Plan，其中换车方案保留了 LP 大于 LD 的原始窗口。

## 两项批准修正

| 情形 | 已核验的最小例 | 实施与依据 |
|---|---|---|
| 全步行路径的资格 | 原始 OD 约 600.45 m，经 S 的两段各约 300.23 m。以 1.4 m/s 步行满足终限 520 s，最终仅返回耗时 100 s 的直达 Car。另一个反例的全步行约 600.45 s，比两个合法方案 1000、1100 s 均快；K=1、2、5 均按正式方案正确排名。 | D011、§3.2 Definitions 2–3、§4.1 假设 (5)。回到 O 形成完整路径时检查正式段数；纯步行在调用 TopK.offer 前退出，不改变名额与成本界。只有步行的部分后缀继续搜索。 |
| 分别校验 LP、LD | 提交 0 s，直达 100 s，替代方案两段分别为 10、110 s，最早 120 s 完成。首段 EP=0、LP=420、LD=410。P1 可提交并执行 400→410 s；405→415 s 在路线提交时原子拒绝，直接执行送达事件也拒绝，乘客仍在车上。 | D002、§3.2 式 (1a–c)。Leg 分别校验 EP≤LP、EP≤LD。M1 输出原始 LP、LD；实际接客和送达各自遵守相应上限。 |

纯步行不会生成可接受的 Plan、绑定资源或产生服务成功事件。混合方案中的相邻步行逐段保存、逐段计时。名义可行性可用从提交时刻开始、按顺序无等待执行的时间表验证：首个正式段开始时刻满足首接截止，链末时刻满足终达截止，由 D002 向后递推可知各段名义结束时刻也满足 LD。现场等待、乘客就绪和实际跨段衔接由提交与执行层核验。

## 调用与数据结构

```python
from src.common.config import load_config
from src.m1 import M1Config, M1Planner
from src.network.prepared import PreparedNetwork

config = load_config("configs/paper_main.yaml")
network = PreparedNetwork.from_config(config)
planner = M1Planner(M1Config.from_config(config))
plans = planner.generate(request, network)  # request 为 P1 Request
```

返回按排名排列的不可变 `tuple[Plan, ...]`，无可行路径返回空元组。`Plan.legs` 顺序包含 P1 的 `Leg`、`WalkLink`。方案 ID 由请求 ID 和本次规划排名组成，段 ID 包含排名和段序。ID 的使用范围是该请求及其固定输入版本。航段携带静态 `route_id`。正式段提供精确名义秒数和 EP、LP、LD，步行提供距离、速度和名义秒数。`step_neighbors(leg_id)` 返回包括步行在内的直接前、后继 ID，端点用 `None` 表示。`formal_leg_count`、`transfer_count`、`service_mode` 与 P1 已有的 `nominal_s` 可直接读取。

**PAPER §3.1、§3.2、§5.1、Fig. 4；DECISION D001、D011。** 请求图用 `("ground", 道路节点ID)` 和 `("air", 站点ID)` 区分层。O/D 与换乘点映射到同一道路节点时共享该地面顶点。不同站点若共用一个道路节点，会触发显式映射冲突，避免通过共享点获得免费站间接驳。每条边保存层端点、物理起终点、种类和精确名义秒数，另建正向和反向邻接表。

| 边 | 数据来源及输出语义 |
|---|---|
| CAR | 对不同地面节点有序对调用 P1 `RoadRouter.shortest_time_s`。遇 `UnreachableRoad` 省略该方向。同一请求的其他可达道路正常参与搜索。直达边与请求的 `direct_road_time_s` 有冲突时报告输入错误。M1 只查询耗时，物理边序列由 P1 的 `shortest_path_edges` 提供给执行层。 |
| WALK | 仅将已证实有向可达的内部接驳按映射道路节点坐标计算球面直线距离，严格小于 500 m 时替换成 P1 `WalkLink`。速度取配置，默认 1.4 m/s。直达 O→D 保留 P0 的整条 OD 筛选结果。 |
| FLIGHT | 取 `AirNetwork.routes` 的方向、固定名义飞行耗时和航线 ID，`station_nodes` 提供执行层端点。不读取航班实例、席位或车辆状态。 |
| TRANSFER | 同站地面点与航空点双向 0 s。仅连接两层，从输出服务链中消去。 |

## 下界、窗口与排序

**PAPER §5.1 式 (20)、Algorithm 2；DECISION D008。** 在请求图上从 O 做一次 Dijkstra，初值为原始提交秒，得到各可达点的绝对最早到达下界。道路最短时间来自 P1；这次搜索的边是 CAR、WALK、FLIGHT、TRANSFER 四种抽象边。随后从 D 沿入边深度优先回到 O，维护当前路径已访问的分层顶点。每次回溯撤销访问标记，允许不同方案复用同一换乘点。

1. **时间下界**。设当前点为 y，已确定的 y→D 后缀耗时为 S。若 y 不可达，或 `earliest_bound(y) + S > D_i`，整条分支不可行。完整候选另核实前导步行完成后，首次正式登乘仍满足请求首接上限。
2. **换乘数**。每个 CAR 或 FLIGHT 使正式段数 L 增加 1。后缀超过正式段上限或 `max(0,L−1)` 超过换乘上限即可剪去。默认 3 段、2 次换乘。人数不参与此判断。
3. **满 K 的成本界**。仅在已有 K 个不同结构的完整候选时，将 `earliest_bound(y) + S` 与提交时刻加最差入选耗时比较。严格大于才剪枝，使等成本结构仍有机会按次序入选。

候选按“名义总秒数、有序结构签名”排序。结构签名逐段包含模式、起终点、固定航线 ID；整数和字符串节点分别稳定排序。转层边不占签名位置，辅助步行保留。相同签名占一个名额，选满后比较当前最大排名，必要时移除该项。人工输入 `[11,12,2]` 加入 10 得到 `[2,10,11]`。15 分钟选项默认关闭，启用即抛 `NotImplementedError`。耗时 1 s 与 1200 s 的两个合法方案均能返回。

**PAPER §3.2 式 (1a–c)；DECISION D001、D002。** `D_i = t_request + direct_road_time_s + delay_tolerance_s`。请求首接上限为 `t_request + pickup_tolerance_s`。两种容忍度由 P1 Request 持有，默认各 420 s。对含步行的有序服务链，按以下规则输出正式段窗口。

| 字段 | 计算 |
|---|---|
| EP | 提交时刻加该段之前所有步行与正式段耗时；无前导步行时首段 EP 等于提交时刻。 |
| LD | 最终时限减该段之后全部名义耗时；包括尾随步行。无步行时等价于从下一段 LD 减去**下一段正向时长**。 |
| LP | 首次正式登乘使用原始首接截止。后续正式段使用该段 LD 减去该段时长，等价于前一正式段 LD 加两段间的辅助步行时间。 |

名义时间沿用 P1 的 Decimal 秒和 `WalkLink.nominal_s`。加法使用 `exact_add`，减法按两数有效位与小数位配置精度。事件执行仍使用 P1 的 `event_second=ceil`。人工案例两条 0.4 s 道路连续行驶，随后步行约 1.112 s。M1 名义总时长约 1.912 s，满足 2.55 s 时限。P1 接受时在换段处从整数事件秒重新起步，最早实际完成为第 3 秒，原子拒绝此次分配。这个差异属于名义可行与执行可行的边界。P2 仅计算就绪下界，实际步行由已接受的服务链触发。

## 缓存与成本

每个规划器持有一个活动网络与配置版本、一个有界请求 LRU。版本包含 P1 道路内容哈希、航空站点映射、有向航线及名义耗时、冻结的 M1 配置。请求键包含 ID、原始提交秒、映射 OD、直达时间和两种容忍度。人数不影响静态拓扑和名义约束。实时位置、步序、航班余座及其他执行状态不参与键。版本变化会清空请求缓存并重建固定换乘图。同配置和同请求命中时返回同一个不可变结果，包括空结果。

默认缓存上限 1024 个请求，可用 `M1Config(cache_size=...)` 或 `m1.cache_size` 设置。被逐出的请求如再次出现，会按相同输入重算。固定道路连接（包括不可达结果）按站点有序对复用；固定航空边随网络版本复用。每个新 OD 仍要查询与站点间的有向路由，密集有向图的简单路径枚举最坏随规模快速增长。

## 微型验证与独立对照

Oracle 从人工物理边表独立建图，用 Bellman–Ford 得到有向地面闭包。随后正向枚举全部简单路径，完成枚举后才按时间和段数筛选并全量排序。Oracle 以 Fraction 加总耗时、独立用前缀和与后缀和计算窗口。直线距离复用基础地理函数；含步行的穷举以 1 m/s 运行。Oracle 不调用 M1 构图、剪枝、窗口、签名或 Top-K 实现。比对项包括每个结构、每段及总成本、正式段数、换乘次数、EP/LP/LD 和完整顺序。

| 人工图条件 | 穷举合法结构数 | 检查规模 |
|---|---:|---|
| 终达容忍 0、5、200 s，最多三段、两次换乘 | 17、20、31 | 分别比较 K=1、2、5、合法数+1 |
| 终达容忍 200 s，最多两段、两次换乘 | 7 | 同上 |
| 终达容忍 200 s，最多三段、零次换乘 | 1 | 同上 |
| 前导、中间、尾随步行与两次飞行组合 | 25 | 全量集合和顺序 |
| 纯步行成本低于所有正式方案 | 2 | K=1、2、5 |
| LP>LD，终达容忍分别为 420、19 s | 2、1 | 各自全部，另一条名义超时的绕行在 19 s 条件下排除 |
| 一段 Car，接车前和下车后可各有两段相邻步行 | 全部合法结构 | 全量集合、各段窗口与顺序 |

| Gate 条款 | 实测证据 |
|---|---|
| M101–M102 | 逐项断言整张有向边表及绝对下界；同一站点对地面正/反为 20/5 s，只有正向航线，孤立站点从 O 不可达。 |
| M103 | 找到直达车、地面多跳和车→飞→车；后者 45 s、3 段、2 次换乘。相邻两段辅助步行不增加正式段数。 |
| M104–M105 | 与全量穷举逐项核对；时限取等号时保留，超出极小十进制量即排除。K 未满能保留成本更高的合法路径。 |
| M106–M107 | 未排序替换、重复签名、等成本签名替换及多条同成本不同结构均有断言。 |
| M108–M109 | 启动未实现过滤器显式失败；默认保留相差 1199 s 的两条路径。覆盖无路、仅一条、仅两条。 |
| M110 | 跨批次只改变就绪秒，命中同一对象且无道路查询；验证容量 1 的逐出、请求时刻变化、航空和道路变化、配置切换及过期直达耗时。 |
| M111 | 10 组条件，27 次对照。Oracle 独立枚举完整路径后按是否含正式段筛选。签名、名义成本、段数、换乘数、窗口、Top-K 顺序全部一致。 |

使用 Conda `ridesharing`，解释器 `D:\Programs_D\Anaconda\envs\ridesharing\python.exe`，Python 3.9.25。NetworkX 3.2.1、NumPy 2.0.2、SciPy 1.13.1、PyYAML 6.0.3、pytest 8.4.2。依赖检查输出 `No broken requirements found.`。

```powershell
$py = 'D:\Programs_D\Anaconda\envs\ridesharing\python.exe'
& $py -B -m pip check
& $py -B -m pytest -p no:cacheprovider -q tests/unit/test_p2_m1.py tests/oracle/test_p2_exhaustive.py tests/integration/test_p2_plans.py
& $py -B -m pytest -p no:cacheprovider -q tests/unit/test_p0_data.py tests/unit/test_p0_pbf.py tests/unit/test_p1_routing.py tests/unit/test_p1_flights.py tests/unit/test_p1_prepared.py tests/unit/test_p1_service.py tests/integration/test_p1_engine.py
& $py -B -m pytest -p no:cacheprovider -q
```

分项输出为 `32 passed in 2.03s` 与 `73 passed in 4.21s`。原有回归包括 P0 的 10 项、P1 的 63 项。相比初版 96 项，修订了 3 项旧冲突断言，另增 9 项。修复前新验收预期复现 7 个失败；最终全量输出为 `105 passed in 4.22s`，失败 0、跳过 0。

## 正式输入限量只读规划

取锁定 EVALUATION CSV 的前 3 条记录，共用一个由 `PreparedNetwork.from_config` 加载的网络和一个 M1Planner。道路文件校验 SHA256 为 `7cf1ed8eeae50a353dd993d43eafcc769fd80715fdbeb471c96a23d23939b8b9`。清单记录道路 18,713 点、26,461 条有向边及 WARMUP/EVALUATION/TAIL 样本 2,490/33,565/18,055；航空网为 10 站、60 条有向航线。可单独复验，命令有 45 s 超时。

```powershell
& $py -B -c "import subprocess,sys; subprocess.run([sys.executable,'-B','-m','tests.performance.p2_readonly_sample'],timeout=45,check=True)"
```

| 请求 | 首次规划 s | 同请求立即命中 µs | 道路耗时查询 / 缓存未命中 | DFS 调用数 | 合法完整路径进入排名次数 | 返回 Plan 数 |
|---|---:|---:|---:|---:|---:|---:|
| 554581 | 1.589288 | 27.4 | 132 / 131 | 164 | 2 | 2 |
| 554583 | 0.434324 | 21.7 | 42 / 41 | 1293 | 11 | 5 |
| 554598 | 0.604630 | 21.1 | 42 / 41 | 3955 | 16 | 5 |

网络加载单计 0.808 s。每次首次规划均未命中请求缓存，首条还建立固定站间连接，后两条复用。计时排除 Request 构造时的一次直达查询；仅在 Pulse 内使用 cProfile 计数，首次耗时包含该计数开销。DFS 调用数包括随后被剪枝的前缀；进入排名次数只统计到达起点并通过资格检查的候选。三次立即命中均返回原结果对象，新增道路查询均为 0。

请求 554581 于第 7200 秒提交，2 人，OD 为 42432589→2653473655。直达方案为 125.1951256444438843 s；经 5020830713 换车的方案为 423.6498350927563952 s，首段 EP=7200、LP=7620、LD=7533.8049172004976755000 s。默认 K=5，搜索只找到两条符合约束的结构，两条均已构造并通过逐段名义时间表断言。

前三条记录集中在同一提交秒，抽样缺乏跨时段代表性。新请求即使复用站间连接，仍有大量道路距离缓存未命中，搜索规模也随 OD 变化。尚无依据估算 33,565 条请求的总耗时、48 小时吞吐量或实际跨批命中率。

## 文件与依据

| 变更文件 | 内容 |
|---|---|
| `src/m1/__init__.py`（改）、`src/m1/config.py`（新） | 公开 API、静态参数及映射冲突类型。 |
| `src/m1/graph.py`（新） | 有向道路闭包、航空层、步行和式 (20) 下界。 |
| `src/m1/pulse.py`（新） | 反向搜索、三种剪枝、Top-K、窗口与领域对象输出。 |
| `src/m1/planner.py`（新） | 版本管理和有界请求缓存。 |
| `src/m2/passengers.py`（改） | Plan 的正式段数、换乘数、模式及前后继只读查询；Leg 分别校验 EP 与两个上限。 |
| `tests/__init__.py`、`tests/p2_examples.py`（新） | 共享人工路网和请求夹具。 |
| `tests/unit/test_p2_m1.py`（新） | 方向、排序、缓存、窗口、阈值及 P1 表达反例。 |
| `tests/oracle/test_p2_exhaustive.py`（新） | 独立物理图闭包、正向穷举和逐项对照。 |
| `tests/integration/test_p2_plans.py`（新） | P1 接受、提交、实际执行及精度边界，连续步行和配置切换。 |
| `tests/performance/p2_readonly_sample.py`（新） | 单独启动的三请求只读检查，核验 554581、查询与搜索规模。 |
| `docs/DECISIONS.md`、`docs/TEST_AND_GATES.md`、`docs/ARCHITECTURE.md`（改） | D002/D011 的兼容性决定、补充验收与接口说明。 |
| `README.md`（改）、`docs/P2_REPORT.md`（新） | 阶段入口及本报告。 |

PAPER 的依据为 §3.1、§3.2 Definitions 2–3、式 (1a–c)、§4.1 假设 (5)、§5.1 Fig. 4、Algorithm 2 和式 (20)。INFERENCE 与 DECISION 对应 D001 的静态航空成本、D002 下一段正向时长及独立窗口、D008 真正的 Top-K、D011 定向拓扑及辅助步行资格、D014 独立测试和运行权限。§5.1 将换乘数写作 cᵢ，与 §3.2 的乘客人数定义冲突，代码使用批准的换乘上限。固定航空拓扑来自批准的 P0 数据。P2 验收范围止于静态规划；下一阶段等待用户授权。
