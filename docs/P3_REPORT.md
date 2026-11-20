# P3 验收报告 · M3 可用运输资源搜索

2026-11-20，Windows，分支 `main`。**P3 Gate PASS**。17 项 P3 轻量测试和 105 项 P0–P2 回归全部通过。D009 的 R-tree 候选及固定航班筛选与独立全扫描一致。已接受请求按原方案继承固定资源。

## 调用与结果

```python
from src.m3 import M3Config, M3Searcher

# 使用同一个 P1 道路路由器构建 Simulator 和 M3Searcher。
searcher = M3Searcher(network.road, M3Config.from_config(config))
snapshot = simulator.batch_snapshot()
resources = searcher.search(snapshot, plans_by_request)
```

`config` 是 `load_config(...)` 得到的配置。`plans_by_request` 是请求 ID 到 P2 `M1Planner.generate(...)` 返回的 `tuple[Plan, ...]` 的映射，可跨批缓存。每次 `search` 用新快照重建车辆 R-tree 和航班查询表。路网版本必须匹配。未提交、首接过期或已终止的请求跳过普通候选搜索。

| `SparseCandidates` 字段 | 供调用者使用的契约 |
|---|---|
| `snapshot_time`、`snapshot_version` | 对应 P1 快照的模拟秒和状态版本。 |
| `by_leg[(request_id, plan_id, leg_id)]` | Type1/2 的正式段对应有序资源 ID 元组。独立的三元组区分跨请求或跨方案重复的段 ID。原 `Leg.mode` 决定 ID 查询 `snapshot.vehicles` 或 `snapshot.flights`。有正式段且无候选时返回 `()`。 |
| `commitments[request_id]` | Type3 的原 `Plan` 及与其所有步骤逐一对应的 `bindings`。WalkLink 原有的 `None` 绑定也保留。执行进度继续从同一 P1 快照读取。 |
| `stats` | 索引构建和 M3 总墙钟秒。Car 查询数、按段截断前候选数、达到上限与实际截断数、入选满载和当前空座不足数。Flight 查询数及总匹配数。两项入选车辆计数以段与资源的候选对为单位。 |

输出及其映射只读。对普通正式段和同模式资源，资源 ID 在候选元组中时，论文 Algorithm 3 的对应 `LS` 元素为 1，其余为 0。Type3 对应固定的单项资源约束，只需读取原方案和绑定；M4 判断尚需执行的任务。前导、中间、尾随和连续 `WalkLink` 均沿用原链，其耗时已进入 M1 窗口。零时长同址转换在 P2 输出阶段消去。M3 查询期间，P1 请求、事件、路线、席位和承诺均保持原状。

## 车辆筛选

**PAPER §5.2 Algorithm 3 第 3–9 行、式 (21) 和 §6.1 Table 4；DECISION D002、D004、D009、D011。** 按原始 M1 `LP` 计算 `r = 6.660896 × (LP − t)` 米，`t` 与 `LP` 均为秒，速度由 14.9 mph 转换，允许配置。`LP < t` 返回空候选。`LP = t` 的方框缩为接客位置，只有与接客点重合且物理容量足够的车辆入选。`LP > LD` 时照常使用原 `LP`，原 `LD` 继续约束实际送达。车辆接送时间与实际载客量由后续解码按 §4.2 式 (8)–(10)、(16) 验证。

- R-tree 对 P1 的 `VehicleState.position` 建立二维点索引。停车位置来自路网节点，在途位置来自快照时刻的边上坐标。当前边终点与真实到达时刻继续留给 M4 的路径检查。每次查询从同一路由器读取接客点坐标。
- 坐标约定是 P1 的经纬度。实现以仓库现有球面半径 `R = 6,371,009 m` 把经、纬弧度乘 `R` 变换为索引轴米制坐标。轴坐标是角度的线性编码。查询框的实距由球面几何确定，P3 的 Manhattan 查询用单个矩形。
- 半径 `r` 对应球面圆角半径 `θ = r/R`。纬度半宽为 `θ`，Manhattan 接客纬度 `φ` 的经度半宽为 `asin(sin(θ)/cos(φ))`。先计算经纬度边界，再按与车辆相同的变换生成 R-tree 查询框。外框含等号。浮点映射重合时再核对原经纬度的框边界。框内圆外候选保留。
- 按球面直线距离升序，其比较使用与距离单调等价的 haversine 值。直接计算原经纬度差，保证对称经度位置的距离平局按车辆 ID 排序。只保留前 `candidate_limit_multiplier × vehicle_capacity` 个。默认是 `2 × 4 = 8`，候选不足时返回实际数量。
- 车辆自身总容量小于请求人数时排除。正在载客、空驶、在途或再平衡的车辆均可入选。当前满载和空座不足只计入诊断。已承诺车辆在空间条件允许时也能成为其他请求的候选，实际接客时的空座和行程顺序由 M4 判断。

**工程实现细节。** 论文 §5.2 同段先描述可达圆，再明确以其外接方框取候选。按 D009 和 P3 任务使用外接框。球面边框与距离排序沿用项目统一球面半径。审查时复现了绝对投影舍入造成的边界遗漏及等距错序，以及局部平面近似遗漏球面圆内候选的情形。三个回归用例覆盖这些边界。

## 航班与历史承诺

**PAPER §5.2 Algorithm 3 第 9–15 行、§4.2 式 (11)–(17)；DECISION D002、D003、D004、D009。** 按有向起降节点建立本批次航班查询表，按 `(depart_at, flight_id)` 排序。有 `Leg.route_id` 时同时要求匹配固定航线，沿用 P1 承诺校验的身份语义。查询只使用仍为 `SCHEDULED` 且 `depart_at > t` 的实例。P1 已在快照前处理 `t` 秒的起飞事件，`EP ≤ depart_at ≤ LP` 与 `arrive_at ≤ LD` 均包含等号。余座取 P1 的 `remaining_seats`，即总容量扣除历史预订。同一余座可让多个新请求各自进入候选，联合占用在 `M415/M309` 由 M4 验收。

Type3 固定继承 `Request.plan` 和 `Request.bindings`。相同资源即使超出地面搜索框、超出普通车辆上限、航班满座或已起飞，仍保留原绑定。传入的 M1 缓存候选只用于普通请求。这个接口可以把已完成的段和后续固定段一并交给 M4，后者通过快照任务和执行前缀识别剩余工作。

## Gate 证据

| 条款 | 证据 |
|---|---|
| M301–M303 | 独立全扫描检查 49 辆车、24 个地面段。另测接客点、等半径边界、圆外框内、框外、LP 过期与零半径、Manhattan 纬度、对称等距 ID 截断。在 P1 真实人工图回放中，第 20 秒在途车辆的边中位置成为最近候选。 |
| M304–M307、D009 T07a/b 的 M3 部分 | 当前满载、载客且空座不足的车辆保留。人工事件回放覆盖四座已满的车在接客点下客释放，再次搜索仍为候选且满载计数更新。总容量不足排除。Type3 车辆位于框外和框内但排名第 10 的两种场景均保留原承诺。实际接客时的拒绝与放行归 M4 验收。 |
| M308–M309 | 与逐航班扫描对照 96 班、24 个飞行段。人工期望覆盖方向、航线、EP/LP/LD 等号、首尾各类越界、状态及历史锁座。两名各需 2 席的新请求可分别得到同一班的最后 2 席，M3 查询前后历史座位一致。 |
| M310 | P1 路线回放时，同一组 M1 缓存对象依次得到停车位置排序、在途位置排序、候选范围收缩及请求到期结果。实际接受、锁座、起飞和降落事件后的连续批次输出按快照更新。 |
| M311 | 独立逐资源扫描使用车辆经线的最小角距离和纬差判断外框，用球面余弦定理排序；航班逐条检查并重算历史占用。逐行核对 48 条正式段的资源 ID、顺序、截断前计数及显式 0–1 矩阵。反转资源映射和请求映射顺序后候选关系一致。 |
| D002、D004、D008、D011 | M1 实际生成 `EP=0, LP=420, LD=410` 的换车方案仍返回候选。连续步行、零时长同址步行连接与尾随步行均不占资源行。P1 在人工图上生成的批次快照在搜索前后相同。 |

环境为现有 Conda `ridesharing`，解释器 `D:\Programs_D\Anaconda\envs\ridesharing\python.exe`，Python **3.9.25**。实测 Rtree 1.4.1、Shapely 2.0.7、NetworkX 3.2.1、NumPy 2.0.2、PyYAML 6.0.3、pytest 8.4.2。`pip check` 输出 `No broken requirements found.`。

```powershell
$py = 'D:\Programs_D\Anaconda\envs\ridesharing\python.exe'
& $py -B -m pip check
& $py -B -m pytest -p no:cacheprovider -q --tb=short tests/unit/test_p3_m3.py tests/oracle/test_p3_full_scan.py tests/integration/test_p3_m3_batches.py
& $py -B -m pytest -p no:cacheprovider -q --ignore=tests/unit/test_p3_m3.py --ignore=tests/oracle/test_p3_full_scan.py --ignore=tests/integration/test_p3_m3_batches.py
& $py -B -m pytest -p no:cacheprovider -q --tb=short
```

P3 专项输出 `17 passed in 1.92s`，P0–P2 独立回归输出 `105 passed in 4.44s`。全量输出 `122 passed in 4.48s`，失败 0，跳过 0。P0 为 10 项，P1 为 63 项，P2 为 32 项，P3 为 17 项。新增测试由 12 项单元、1 项独立对照和 4 项跨模块集成组成。

## 人工批次计时

固定种子 `20261120`，一个人工快照含 96 辆车、64 个航班实例、28 条请求，合计 28 个 Car 段和 28 个 Flight 段。在同一进程内搜索 5 次，各次重建索引，候选及非计时统计完全一致。

```powershell
& $py -B -c "import subprocess,sys; subprocess.run([sys.executable,'-B','-m','tests.performance.p3_synthetic_batch'],timeout=20,check=True)"
```

| 指标 | 最小 / 中位 / 最大 (ms) |
|---|---:|
| R-tree 构建，含车辆坐标转换 | 0.274 / 0.292 / 0.528 |
| M3 总墙钟 | 1.311 / 1.440 / 24.192 |

Car 截断前候选数为 1–32。截断后候选为 1、5、6、8 辆的段数分别是 1、3、3、21，21 个段实际截断。共有 44 个满载车候选对，111 个当前空座不足的候选对。Flight 候选为 1、2、3、4、5 班的段数分别是 4、5、8、8、3，共 85 个匹配。空座不足与满载的实际选用率需要后续 M4 分配结果和同一快照联合计算。

## 文件与待验收边界

| 文件 | 变更 |
|---|---|
| `src/m3/__init__.py` | 修改，公开 M3 API 和结果类型。 |
| `src/m3/config.py` | 新增，读取现有 M3 配置、设定速度及候选容量倍数。 |
| `src/m3/search.py` | 新增，批次 R-tree、航班索引、稀疏候选、固定承诺及统计。 |
| `tests/unit/test_p3_m3.py` | 新增，人工期望、边界、截断、占用和模式覆盖。 |
| `tests/oracle/test_p3_full_scan.py` | 新增，独立车辆与航班全扫描、显式 LS 对照。 |
| `tests/integration/test_p3_m3_batches.py` | 新增，P1/P2 实际接口、跨批次车辆和航班状态验证。 |
| `tests/performance/p3_synthetic_batch.py` | 新增，有界人工样本计时与候选分布。 |
| `docs/TEST_AND_GATES.md` | 修改，写明后续 M4 联合席位验收的最小案例。 |
| `docs/P3_REPORT.md` | 新增，本报告。 |

D003 的实际接客、当前边续驶、换乘就绪、固定航班到达时限，以及式 (14)–(17) 的联合可行性均需后续解码验证。车辆方框和八车上限属于候选筛选，可能保留实际不可执行的资源，也可能截去可执行的较远资源。候选多时须对所有通过方框和总容量筛选的车辆排序。航班索引成本随快照包含的航班实例增大，人工样本无法估计正式吞吐量或高负载批次延迟。

本轮复用了 P1/P2 现有接口和现有依赖。正式数据仿真执行次数为 0，Git 提交及推送次数为 0。按 D014，正式仿真由用户手动启动。本轮实施范围为 P3，后续阶段待独立授权。
