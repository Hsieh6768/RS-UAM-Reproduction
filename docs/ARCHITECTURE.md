# 架构、对象、接口与因果时间契约

## A. 固定目录（已批准）

```text
RS-UAM-Reproduction/
  AGENTS.md README.md pyproject.toml .gitignore
  references/paper.pdf
  docs/                          # 设计、验收、命令手册
  codex_tasks/                   # 一阶段一张任务卡
  configs/                       # base、短窗、5h、48h、诊断
  src/
    __init__.py
    common/                      # 类型/单位/配置/RNG/状态码
    network/                     # OSM有向道路、航空拓扑、统一最短路
    data/                        # 原始数据审计、清洗、地图匹配、分层抽样
    m1/                          # Pulse、候选方案、缓存
    m2/                          # Request/Vehicle/Flight和快照状态
    m3/                          # R-tree、候选车辆/航班、稀疏LS
    m4/
      encoding.py                # 两层编码和出现次序映射
      decoder.py                 # 全量参考和分量解码公共物理内核
      task_graph.py              # 资源关联/时序依赖与弱连通分量
      operators.py               # 七算子
      halns.py                   # 退火、轮盘、评分、预算
      validation.py              # 可行性与修复
    m5/                          # 运输模型、网格流具体化、再平衡
    simulation/                  # 事件队列、时钟、原子提交
    evaluation/                  # 请求和性能指标、输出、汇总
    cli/                         # python -m src.cli.main
  tests/{unit,oracle,integration,regression,performance,fixtures}/
  data/raw/                      # P0生成，禁止修改
  data/processed/                # 产物带manifest
  outputs/                       # 每次run独立
```

`src/` **本身就是 Python 包**（不是 `src/uam`）；使用小写 `m1`–`m5` 适配 Python 的导入习惯，但各模块均分文件夹。
不要将所有功能集中在一份 `main.py`。

## B. 领域对象建议

- `Request`: 稳定ID、提交时间、原始/映射OD、人数、容忍时间、方案集合、cohort(`WARMUP`,`EVAL`,`TAIL`)、状态、已接受方案ID、最终完成时刻。
- `Plan`: request_id、按顺序的若干正式运输 `Leg` 与辅助 `WalkLink`，名义时长、静态窗口、签名。
- `Task`: 稳定ID、request_id、leg_id、`CAR_PICKUP`,`CAR_DROPOFF`,`FLIGHT` 等枚举、资源ID、时限与先后关系。
- `VehicleState`: ID、容量、车上人数与乘客ID、当前路节点/道路边、边起止事件时间、待执行承诺/再平衡目标、route_version。
- `FlightInstance`: 有向航线、站点、固定起降时刻、容量、锁定席位、剩余席位。
- `BatchSnapshot`: batch_time、冻结真实状态、仍有效Type1/2 + Type3、方案缓存、可用航班、前轮安全基础解、图版本。
- `SolutionEncoding`: 第一层请求方案+各段资源；第二层重复请求ID的顺序；任务出现次序映射。
- `DecodedSchedule`: 稳定任务起止时刻、车辆路线顺序、航班占用、候选被删除请求、(f1,f2,v)、是否可行与原因。
- `AssignmentDecision`: 已选方案、运力、具体合法任务序列、预计时间、锁定承诺与指标。
- `RebalanceDecision`: 网格运输整数流、车辆ID/目标路节点/路线。
- `SimulationEvent`: `(event_time, event_priority, seq, version, event_type, ids)`。
- `ValidationReport`: passed/failed、错误码、关联对象、错误字段与实际/期待值。
- `RunManifest`: 代码版本、完整参数、原始/处理数据哈希、拓扑哈希、种子、硬件、库版本和运行时段。

## C. 公共接口契约（目标接口，供Codex实施/改进）

```python
class RoadRouter:
    def shortest_time_s(self, source_node: str, target_node: str) -> float: ...
    def shortest_path_edges(self, source_node: str, target_node: str) -> list: ...

class M1Planner:
    def generate(self, request, immutable_network) -> tuple[Plan, ...]: ...

class M3Searcher:
    def search(self, batch_snapshot, plans_by_request) -> "SparseCandidates": ...

class FullDecoder:
    def decode(self, immutable_batch_snapshot, candidate_encoding) -> "DecodedSchedule": ...

class PartitionedDecoder:
    def decode(self, immutable_batch_snapshot, candidate_encoding) -> "DecodedSchedule": ...

class M4HALNS:
    def solve(self, immutable_batch_snapshot, plans, candidate_resources,
              previous_feasible_baseline, operator_scores, rng, wall_deadline
              ) -> "AssignmentDecision": ...

class M5Rebalancer:
    def propose(self, committed_snapshot, unmet_requests, truly_idle_vehicles
               ) -> "RebalanceDecision": ...

class Simulator:
    def advance_to(self, simulation_time_s: int) -> None: ...
    def snapshot(self) -> "BatchSnapshot": ...
    def commit_atomically(self, checked_plan) -> None: ...

class ScheduleValidator:
    def validate(self, immutable_batch_snapshot, proposed_schedule) -> "ValidationReport": ...
```

上表描述各阶段的数据流。已实现的 P1 签名、道路节点类型、Decimal 秒、不可达异常、快照和单车提交边界见 [P1 公共接口与验收记录](P1_REPORT.md)。完整调度结果提交由后续阶段接入。
所有M4候选操作在独立工作区进行，不修改`VehicleState`等真实对象。

M1 的结果按名义成本与结构签名排序。每个 Plan 至少含一个 Car 或 Flight，辅助步行按链中顺序消耗时间。Leg 的 LP 与 LD 分别用于接客和送达，允许 LP>LD；下游使用各自的原始上限检查实际事件。详见 [P2 接口与验收报告](P2_REPORT.md)。

## D. 仿真时间与事件次序

- 每10秒生成一次调度事件；事件引擎实现`advance_to(t)`，按 `(event_time, event_priority, seq)` 排序处理所有 `<=t` 事件。
- 10:00:17过道路边、10:00:18接客可在10:00:20批次前集中回放，但记录的真实时刻仍是17/18秒。
- 不需要每秒修改位置；车辆在道路边上的中间位置根据边起止时间线性插值得到。到下一次批次时可直接查询准确进度。
- 正在驶过有向道路边必须先到达边终点再执行新路线；接客等事件的真实时刻独立于10秒调度边界。
- 当前批次新请求如恰好在`batch_t`到达，应在取快照之前被加入；**未来才到达**的请求不得参与。
- 事件同刻优先级须确定并测试：先完成旧物理事件（道路边到达、下客、航班降落/既有换乘就绪等），随后处理截至t到达的新请求及过期，再生成调度快照；具体同刻上/下客与航班登机边界须由P1交付测试表，不得隐藏。
- 调度计算耗时不改变模拟时钟，批次t的合法决策于t即时提交。
- `route_version`标记过时的未来**计划**事件失效；不能删除已经执行或当前道路边的完成事件。

## E. 时刻、时间窗、路网和评分

- 所有原始日期解析为纽约当地日历时区一致的时间（2013年5月当地为EDT），用相对仿真起点的秒/整数刻度表示；杜绝夏令时与UTC混用。
- 路边长度以m计、车速6.660896m/s、飞行速度75.9968m/s；道路耗时以float秒计算，编码/事件采取单一确定性量化。禁止每边先取整。
- P6 按配置绑定道路实例。D011 历史配置使用完整 Manhattan 有向图，D016 配置使用最大 SCC 和双端 100 m 样本。两者分别绑定请求、道路权重和起降场映射，M1–M5 与提交器共享该次运行的路由器。
- M1 跳过道路不可达的抽象连接。M4 将包含不可达道路段的候选判为不可行，保持已提交状态。车辆仅沿存在的有向路径产生移动事件。M5 的目标若无法分配到具有可达路径的车辆，则该移动不生成，保留合法 M4 承诺。道路不可达不得用零时间、补边或瞬移替代。
- 请求首接截止=原始 t+420s；终达截止=原始 t+静态有向道路直达最短时间+420s。
- f1包含候选当前全部满足请求（包括已接受未完成）；f2是这些请求的原始提交至预计最后到达的合计**分钟**。
- 使用`current`比较退火，`best`只在当前批次更好合法候选产生时更新；跨批次必须重新评分安全基线。

## F. 提交与回退

1. `advance_to(batch_t)`完成真实旧事件并取不可变快照。
2. `M3`为未承诺行程取得资源候选，同时保留Type3的固定承诺。
3. `M4`从安全基础解及随机初始解探索，候选重排/修复不能修改真实状态。
4. 搜索到期按`best`返回；使用现有安全基础解是正常策略，不是异常。
5. 调试模式可启用`ScheduleValidator`独立复核；正式模式关闭昂贵复核，但基本物理合法性检查仍常开。
6. 确认快照版本有效后原子提交新方案；完成当前行驶边再重定向；旧未来事件版本失效。
7. `M5`基于提交后的真实空闲车/未满足请求提案，不能窃取承诺车辆。
8. 新路由事件/乘客状态均保留可追溯ID。
9. 若违反运行时不变量，则`FAILED`并保留快照/异常，不使用安全基线掩盖程序错误。

## G. 特别注意任务网络和搜索

- 任务**关联**边与真实**时间先后**边区分；同航班资源共享可能有双向关联，但不应被拓扑排序成非法环。
- 完整解码与分解解码对同一合法编码必须给出相同任务时刻/容量/得分；修复冲突时要固定一致的冲突选择顺序或在差异日志中明确。
- Type3方案和资源固定，未执行任务允许合法重排；论文"first four"含糊措辞与用户的D004/D007决定见追踪表。
- 15分钟额外Pulse过滤只保留接口、不实现。
- noD必须取消分解搜索范围，而非仅改变decode调用。
