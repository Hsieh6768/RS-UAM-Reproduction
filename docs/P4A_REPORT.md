# P4-A 验收报告 · M4 完整解码与安全基础解

2026-11-20，Windows，分支 `main`。**P4-A Gate：PASS**。24 项 P4-A 专项、122 项 P0–P3 回归全部通过。联合席位、不可中断道路边、Type3 既有时刻以及共享车辆的六种任务顺序有人工期望。验收针对批次候选计算和人工事件回放。真实数据时窗仿真、依赖变更、Git commit/push 次数均为 0。

## 交付文件

| 文件 | 变更和职责 |
|---|---|
| `src/m4/__init__.py` | 修改，公开编码、全量解码、结果、独立校验器及异常。 |
| `src/m4/encoding.py` | 新增，第一层选择、第二层出现序列、P1 任务身份和出现次数校验。 |
| `src/m4/baseline.py` | 新增，从快照核查历史、车载成员、固定预订、剩余行驶和预约，恢复基础序列，留存失败现场。 |
| `src/m4/decoder.py` | 新增，完整时间传播、联合容量、整请求修复、安全基础解、随机初始化。 |
| `src/m4/result.py` | 新增，不可变任务表、逐段时刻、冲突记录、目标函数及同批次比较。 |
| `src/m4/validation.py` | 新增，独立核对服务链、实际路径边、资源占用和评分。 |
| `tests/p4_examples.py` | 新增，用 P1 的 Simulator 和 P3 的 M3Searcher 建立人工批次。 |
| `tests/unit/test_p4a_decode.py` | 新增，非法编码、窗口、整请求修复、目标、RNG、快照版本和航向。 |
| `tests/oracle/test_p4a_timeline.py` | 新增，混合服务链、手算车辆顺序、小数秒和校验器反例。 |
| `tests/integration/test_p4a_commitments.py` | 新增，P1–P4 批次衔接、Type3 保护、共享席位、预约回放和历史一致性。 |
| `docs/P4A_REPORT.md` | 新增，本报告。 |

## 依据与接口

| 依据 | 实现范围 |
|---|---|
| PAPER §4.2 式 (2)、D005、D007 | 按本批可完整服务的请求数及从原始提交到最终送达的分钟数评分。 |
| PAPER §5.3.2、Fig. 5、D003、D006 | 第一层固定方案及正式资源，第二层重复请求 ID。式 (22a,b)、(23a,b) 计算同车任务与跨段接续。 |
| PAPER §5.3.2、D004、D005、D009 | 新请求和积压请求整单修复。保留已接受请求的方案和运力，联合核算新座位与历史预订。 |
| PAPER §5.3.1、Algorithm 4 第 1–4 行、§5.3.4 | 先建立 Type3 剩余编码，再对新请求选第一方案、随机选资源并逐次抽取仍有待排任务的请求。D005 在当批独立评分安全基础解。 |
| DECISION D002、D004、D011、D014 | 原始 EP/LP/LD、P1 绝对事件秒、辅助步行、历史与真实状态隔离、独立验收和异常留证。 |
| PAPER §5.3.3、D006 | 同航班乘客共享容量。在全量解码中，乘客之间没有相互等待的因果边。弱连通分解属于 P4-B。 |

入口使用同批次的真实接口。例中的 `plans` 是请求 ID 到已按名义时长排序的 P2 Plan 元组的映射。

```python
from random import Random
from src.m3 import M3Searcher
from src.m4 import FullDecoder, ScheduleValidator

snapshot = sim.batch_snapshot()
sparse = M3Searcher(sim.router).search(snapshot, plans)
decoder = FullDecoder(sim.router, snapshot, plans, sparse)
initial = decoder.initialize(Random(23))
result = initial.best_solution
ScheduleValidator(decoder).validate(result)
```

`Encoding.choices[rid] = Choice(plan_id, resources)` 按正式 Leg 顺序存储资源 ID。将其与 `decoder.selected_plan(rid, choice)` 的正式段配对即可取得 `(rid, plan_id, leg_id, mode, resource_id)`。普通绑定逐一核对 `SparseCandidates.by_leg` 和相应种类的实体表。Type3 使用 `commitments`，并逐项与 P1 Request 的 Plan 和含步行占位的 bindings 核对。WalkLink 保存在 Plan 顺序中。

`Encoding.sequence` 的第 n 次出现映射到该请求第 n 个**尚待执行的正式任务**。Car 展开为 PICKUP、DROPOFF，Flight 展开为一个 FLIGHT，步行消耗零个出现位置。身份沿用 P1 的 `task_id(request_id, leg_id, kind)`。多余、缺少或未知的出现、未知方案以及无效普通绑定抛出 `EncodingError`。候选移除或替换 Type3 的第一层选择会返回带 `changed_commitment` 的无效结果。

## 物理时间与容量

1. 按第二层顺序处理事件。每辆车维护当前可用节点、物理可用时刻、车上请求集合和人数。每位乘客维护自己的服务链游标。相互独立的车辆可在同秒工作。
2. 车辆停驻时从当前节点和批次秒出发。在途时从当前边终点和 `motion.exact_end` 接续。两次任务之间查询 P1 的有向 `shortest_time_s`，用 `exact_add` 累加物理时间，最后用 `event_second` 取绝对时刻的上整秒。接送或停等结束后从该整数执行秒重新起步。不可达产生 `unreachable_road`。
3. 接客取车辆抵达、乘客实际就绪和 EP 三者的最大值，独立检查首次登乘截止和 LP。下客使用该车辆前一任务的位置和时刻，检查车载成员、乘客最低行驶时长、LD 及请求最终截止。车上人数在每次接送时按整组人数更新。同秒下客可释放座位供同秒接客。若编码要求同车某次接客先于同站下客，后者最早在下一执行秒发生，以遵守 P1 的同秒下客优先级。
4. 各 WalkLink 从乘客就绪时刻开始，以 P1 的单段步行执行秒结束。连续步行逐段接续，尾随步行的结束秒就是最终完成秒。Type3 的已完成前缀来自快照。进行中的飞行只预测原定降落，进行中的步行保留已经执行的开始秒。
5. 航班核对状态、有向端点、指定航线、固定起降及名义飞行时间。到站时刻等于起飞秒可登机，晚到直接冲突。每次解码从快照逐班复制 `{请求 ID: 人数}`，历史订座已经计入总数。新增请求按编码到达该航班任务的次序占座，容量比较包含此前新增的所有整组乘客。

## 候选修复、基础解与初始化

每次试算首先验证固定选择，再按请求 ID 排序建立服务链，随后按第二层从左至右处理正式任务。遇到的第一个硬约束冲突决定本次处理对象。Type1/2 从两层删除整项分配，所有乘客游标、车辆队列、航班临时预订和时间表都从原始快照重建。一次删除严格减少一个请求，因此循环有限。结果记录按次序排列的 `repairs(request_id, reason, detail)` 和 `attempts`。Type3 冲突终止整个候选，返回 `feasible=False`、失败对象和空评分。

安全基础解核对历史任务前缀、已登乘/完成时间、当前段、车上整组人数、固定班次及预订，并检查 P1 的剩余边和原预约程序。按每个乘客的顺序以及每辆车已排定的顺序恢复合法线性序列。已经预约的发车、接送等待和服务秒得到保留。普通候选在同一快照上可以合法提前或重排尚未执行的地面服务。`decode(base.encoding, preserve_appointments=True)` 可复算原基础解。

P1 允许先接受请求、后逐车填入预约。对于未排全的车辆任务，基础解按现有先后关系确定顺序，并用完整解码验证。对于同车多请求，若剩余顺序影响可行性且当前确定性补全失败，抛出 `BaselineOrderRequired`，并保存证据。调用者可以传入上一轮剔除已执行前缀的 `remaining_sequence`；它必须满足所有已有车辆预约顺序。测试中两组各四人共用一辆车，只有先服务 LP=2 s 的 B 才满足承诺；传入 BBAA 后 B、A 分别在 5、15 s 完成。快照中确证的历史冲突抛出 `SnapshotError`。

初始化优先选取每个 Type1/2 的第一条 M1 方案。若该方案有正式段缺少候选，该请求此次未选入。其余正式段用显式传入的 RNG 逐段选资源，再反复从仍有待排任务的请求 ID 中等概率抽取一个。重复种子 23 可重放，种子 41 在多车人工例产生不同编码。`random_candidate` 保留随机试算及其诊断。`current_solution` 取合法随机候选，失效时使用 `safe_baseline`。`best_solution` 取同批两份合法结果的高分者，相等时保留基础解。

## 人工核对

混合链使用车辆 x、y、一个 12→16 s 的固定航班，以及请求 A（两人）、B（一人）。A 含三段正式服务及四段步行，B 有一段 Car。第二层为 `ABABAAA`。

| A 的服务段 | 实际就绪 s | 接客 / 起步 / 起飞 s | 送达 / 降落 s |
|---|---:|---:|---:|
| 前导步行 | 0 | 0 | 2 |
| Car x | 2 | 2 | 4 |
| 站间步行 | 4 | 4 | 7 |
| 固定航班 | 7 | 12 | 16 |
| Car y | 16 | 16 | 18 |
| 尾随步行一 | 18 | 18 | 19 |
| 尾随步行二 | 19 | 19 | 21 |

x 的四次任务依次为 A 接、B 接、A 送、B 送，执行秒为 `2, 2, 4, 4`，事后车载人数为 `2, 3, 1, 0`。B 在 4 s 完成。两人组 A 占两席，对请求数贡献 1。两请求均在 0 s 提交，手算 `f1=200`、`f2=(21+4)/60` 浮点分钟、`v=139.875`。独立单请求例 600 s 得 `f1=100, f2=10, v=67`。目标采用 `β=0.7, δ=100`，起点永远是原始提交时刻。比较接口要求同一批次求值上下文。全局成功数由正式评价按唯一请求统计。

| 条款 | 其他独立期望及结果 |
|---|---|
| M401–402、CORE07/11 | Car 两任务，Car→Flight→Car 五任务。车上 H 仅剩一次下客；已起飞的 H 零个待编码任务，仍按原班降落并参与本批评分。 |
| M304、M415 | 在途满载四人，H 于 20 s 下客释放四席，A 同秒上两人，于 120 s 下客。把 A 接客移至 H 下客之前得到 `vehicle_capacity`。 |
| M309、CORE12 | 四座航班 H 已锁两席。A 或 B 单独加入均可；H/A/B 同时分配时 B 整单删除，最终账本 H=2、A=2。H、A 同为 20 s 登乘、25 s 降落。 |
| M407、M418 | A 接驳耗时 8 s，赶不上 5 s 航班；B 先占该航班，随后车辆无法沿有向路到接点。固定结果依次删除 A、B，试算三次，最终该班临时座位和两车队列全空。Type2 在另一候选中仍可用可达车辆完成。 |
| M408、CORE08 | 候选引入新请求绕行，已接受 H 的实际下客超过 LD。整份候选无效。原乘客、预订、车辆预约、版本及已执行历史可继续读取。 |
| M409、CORE09–10、M413–414 | 有合法随机候选因总出行时长而得负分；current 保留该候选，best 保留得分 0 的基础解。相同编码跨批重新计算后原始提交时刻仍为 0，最终时刻从 45 变为 55 s。 |
| CORE05/13/16 | 正在走的边在 1.2 s 结束，再行驶 0.75 s，绝对时刻 1.95 s 对应执行秒 2；首车 2→7 s，次车等至 7 s 接客、14 s 下客。 |
| D002、CORE14–15 | `LP=420, LD=410` 下 400→410 s 合法，405→415 s 触发送达超时；另例 11 s 接客超过 LP=10，即使 LD=100 也拒绝。 |
| 式 (22)–(23) | 两请求共车的全部六种有效出现顺序，逐项比对手算停靠秒和车载人数。原预约另例在 10、25 s 发车，14、32 s 到站，20、40 s 服务；P1 实际回放于 40 s 完成。 |

## 隔离、验证和可复验命令

P4-A 的生产入口只接收 RoadRouter、冻结的 BatchSnapshot、M1 方案和 P3 稀疏结果。每次尝试只写自己的临时对象，返回冻结记录、元组及分层只读映射。测试检查传入可变编码容器清空后的防御性复制、对嵌套座位账本写入失败，以及候选成功、修复或违约后的 P1 快照和历史相等。版本不匹配的 M3 结果在求值前报错。

`ScheduleValidator` 从给定结果独立核对乘客链和缺失/多余任务，以实际最短路径边和 Fraction 累计物理时间，逐停靠重算车载成员，重建航班席位和目标值。它不调用解码器试算。故意篡改评分、座位归属、车辆顺序、步行开始秒或插入幽灵航班任务，均抛出 `ValidationError` 并留下 FAILED JSON。删除快照中 H 的既有航班预订，也在基础解构造时产生有快照时刻与原因的 FAILED 诊断。默认诊断目录为 `outputs/p4a_failures/`，异常向调用方传播。D014 的真实运行模式切换与整次实验失败退出由后续仿真入口接入。

环境为 Conda `ridesharing`，解释器 `D:\Programs_D\Anaconda\envs\ridesharing\python.exe`，Python **3.9.25**。实测 pytest 8.4.2、NetworkX 3.2.1、Rtree 1.4.1、NumPy 2.0.2、Shapely 2.0.7、PyYAML 6.0.3。`pip check` 输出 `No broken requirements found.`。首次专项收集在 M4 公共接口尚未实现时按预期报三个 ImportError。开发中的人工预约反例检出了最早可达时刻与原程序实际抵达时刻的差异；修正后专项及全量复验均通过。Windows 沙盒下 pytest 的临时目录曾触发 WinError 5，以下通过结果来自使用正常临时目录的同一解释器。

```powershell
$py = 'D:\Programs_D\Anaconda\envs\ridesharing\python.exe'
& $py -B -m pip check
& $py -B -m pytest -p no:cacheprovider -q --tb=short tests/unit/test_p4a_decode.py tests/oracle/test_p4a_timeline.py tests/integration/test_p4a_commitments.py
& $py -B -m pytest -p no:cacheprovider -q --ignore=tests/unit/test_p4a_decode.py --ignore=tests/oracle/test_p4a_timeline.py --ignore=tests/integration/test_p4a_commitments.py
& $py -B -m pytest -p no:cacheprovider -q
```

| 测试范围 | 实际输出 | 失败 / 跳过 |
|---|---|---|
| P4-A 专项 | `24 passed in 2.30s` | 0 / 0 |
| P0–P3 独立回归 | `122 passed in 5.04s` | 0 / 0 |
| 全部 | `146 passed in 5.03s` | 0 / 0 |

开发期保留了一份人工红测诊断 `outputs/p4a_failures/failure-16173a2337514ae089f53f80d33c2a21.json`，记录旧实现将两个未排预约的四人组连续接上同一辆车而触发的容量冲突。修正后的同例按 AABB 服务，两组在 2、6 s 完成。`outputs/p4a-tests-01/` 是临时目录权限排查时生成的 pytest 目录。

## 接口边界与剩余风险

- P4-B 可读取 `encoding, tasks, vehicles, final_nodes, flight_seats, trips, completion, repairs, attempts, failure, f1, f2, v` 作为同批全量参考。每次结果含 `wall_s`、`cpu_s`。冲突处理、同秒优先级和是否保留原预约都影响对照结果。
- 目前每次整请求修复都从头解码。若连续移除 k 个请求，会试算 k+1 次。历史一致性检查、重复的有向路查询、全班次账本复制及独立验收均有成本。人工图耗时不足以证明正式负载下的 10 s 预算。墙钟预算、算子、退火、分解一致性和耗时分位数验收留给 P4-B。
- P1 提供单请求接受、单车路线替换和版本检查。P6 联合提交应根据同一快照版本，一次复核并发布整批方案/资源、车辆任务序列与航班预订；部分预约状态须携带上一轮剩余顺序。P4-A 只交付候选结果。
- 使用的正式输入沿用 P0 身份。道路 18,713 节点、26,461 条有向边，航空 10 站、60 条有向航线，WARMUP / EVALUATION / TAIL 为 2,490 / 33,565 / 18,055。阶段开发期间未清洗、抽样或改写正式输入。真实 10/30 分钟、五小时及两天负载均待用户手动运行。

按任务卡，M401–402、M407–409、M413–415 及对应 CORE 条款已有本阶段或已复验的 P1–P3 测试证据。M403–406、M410–412、M416–417、M419–422 中的搜索、分解、预算及最优界条款属后续阶段。当前授权止于 P4-A。
