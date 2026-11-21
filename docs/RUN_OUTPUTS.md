# 控制台输出、实验文件和性能统计契约

## 1. 控制台：每批一条可读摘要

目标格式（数据纯示例，不是实测）：
```
[2013-05-05 08:30:10][Batch 3782][EVAL] new=5 pending=19 accepted+=3 expired+=1 |
fleet idle=12 busy=34 rebalance=4 | M4 wall=243.5ms cpu=239.8ms |
candidates=450 components=7 best=827.40 fallback=no | batch=262.1ms
```
- 所有真实数据10/30分钟、5小时和48小时实验由用户手动启动；用户可实时观察每批输出。
- P6 每批一条摘要，含累计评价接受/完成数，终端和 `console.log` 同步刷新。最后一列 `total` 计到写本行摘要前。
- 大规模逐候选日志禁止直接逐行输出；保存错误批次完整诊断而不是刷屏。
- Ctrl+C时尽可能写入`INTERRUPTED`标志与已完成统计，不把部分结果伪装成完整论文运行。

## 2. 每次独立输出目录

`outputs/<run_id>/`至少包括：
```
manifest.json
config.yaml
console.log
summary.json
summary.md
batches.csv
requests.csv
vehicle_events.csv.gz
flight_usage.csv
performance.json
audit.json
```
允许补充`errors/`、`snapshots/`、`figures/`、`operator_stats.csv`、`rebalance.csv`，但最小产物不可缺。
大量事件使用流式gzip；实验结束时压缩和flush完成，并统计行数/哈希。
对于发生错误的运行，也需保存诊断、错误码、最后成功批次、快照与异常堆栈。

## 3. `manifest.json`最小字段

`run_id, status, start_wall, end_wall, code_commit, dirty_tree, python_version, gurobi_version,
os, cpu_model, configs_sha256, raw_tlc_sha256, processed_sample_sha256,
road_network_sha256, air_routes_sha256, seed_data, seed_fleet, seed_halns,
mode, vehicle_count, cohort_window, warmup_window, tail_policy,
m4_wall_budget_seconds, validator_enabled, input_counts, output_checksums`。
所有用户确定的工程偏差需通过`method_deviations`字段关联设计登记项。

## 4. `batches.csv`最小列

`batch_time, cohort, new_req, type1, type2, type3, accepted_new, expired, pending_after,
fleet_idle, fleet_busy, fleet_rebalancing, flights_reserved, graph_components,
largest_component_tasks, m4_wall_ms, m4_cpu_ms, batch_wall_ms,
candidates_evaluated, repair_count, sa_accept_worse_count, best_score, used_fallback,
m4_wall_budget_exceeded`。

## 5. `requests.csv`最小列

`request_id,cohort,original_request_time,orig_lat,orig_lon,dest_lat,dest_lon,
mapped_origin,mapped_dest,party_size,source_record_id,accept_time,
actual_first_pickup,actual_final_dropoff,state,plan_id,service_mode,
base_direct_road_time_s,actual_total_time_s,detour_time_min,
is_in_eval,final_counted_success,unserved_reason`。
辅助步行的开始/结束也应能由事件记录追溯。

## 6. `summary.json/md`最少统计

- 预热/评价/尾部样本数，必要字段异常过滤数量与比例。
- 评价请求数、已接受/已完成/未满足/过期、服务率和`accepted=completed`验收结果。
- 平均绕行时间、平均完整旅行、平均等待、负绕行人数。
- SHRS/MHRS/RS-UAM各自数量与占比。
- 分车队状态/里程、空驶/再平衡、航班座位使用率。
- 每批M4墙钟/CPU均值/中位/p95/p99/max/超时；整个批次耗时同样统计。
- 任务分量大小/修复次数/算子使用与奖励；不同算子细节可另存文件。
- 日/小时服务率、请求集中度与尾部窗口统计。
- 代码/数据/随机性/环境哈希和与论文数值相比的偏差说明。
- status为FAILED或INTERRUPTED时**不能产生伪装成COMPLETE的主结果**。

### P6 已实现的字段与读法

| 产物 | 字段与单位 |
|---|---|
| `manifest.json`、`config.yaml` | 实际生效 YAML、参数哈希、UTC 创建与结束时间、Python 路径与版本、Conda 环境、Git HEAD、工作树状态及当前 `src/**/*.py` 内容指纹。真实运行记录已准备道路图、各样本哈希与覆盖期；原始 TLC、OSM、静态航线指纹来自 P0 已核验的来源清单。记录初始车位、评价窗口独立分母和结果文件 SHA256。 |
| `console.log`、`batches.csv` | `batch_time` 为距预热起点的整秒，`sim_datetime` 为纽约日期与时间。`type1/2/3` 为搜索前分类；`accepted_new/expired` 为本批真实状态变化，`pending_after/accepted_incomplete` 为发布后的存量。四种车辆计数及 `evaluation_accepted/completed` 可直接核对。 |
| M1/M3 批次字段 | `m1_planned/m1_cache_hits/m1_active_cache_size`、`m1_wall_ms`；`m3_wall_ms/m3_index_ms/m3_car_queries/m3_car_legs_truncated/m3_flight_candidates`。道路 OD 时间/路径缓存分开记录累计命中、未命中和当前大小。 |
| M4 批次字段 | `m4_wall_ms/m4_cpu_ms` 为算法预算计时，`m4_call_wall_ms/m4_call_cpu_ms` 为调用入口至返回、含可选终检。`validator_wall_ms` 单列。`budget_exhausted/m4_stop_reason/m4_wall_overrun_ms` 解释停止原因与协作式越界。`candidates_started/evaluated` 排除安全基础解；`decode_count` 包括基础解和已完成候选的修复重解码。分量、任务、算子次数、奖励、评分及修复原因随批次保存。 |
| M5 与整批字段 | `m5_status/m5_reason/m5_detail`、供需、未覆盖数、运输流、不可达诊断、求解与配车耗时。`rebalanced` 为实际发布的跨格车数。`batch_wall_ms` 包含事件、模块、终检、提交、计数及事件写入；批次行自身的输出/终端刷新发生在该时间戳之后。`batch_wall_budget_exceeded` 单独与配置的论文参考秒数比较。 |
| `vehicle_events.csv.gz` | 流式 CSV 含 `time,kind,seq,object_id,vehicle_id,route_version`；每个实际道路边记录两端、way ID、同向平行边序号、米数、秒数、精确物理完成时刻和用途。也记录请求到达、截止检查、上下车、辅助步行及共享航班事件。截止检查本身不代表过期人数。 |
| `requests.csv` | 每个引入系统的原始请求一行。`original_request_time` 为当地日期时间，`original_request_second/accept_time/actual_first_pickup/actual_final_dropoff` 为相对预热的秒数；坐标、映射 OD、人数、plan ID、bindings、终态及服务模式随行输出。`actual_total_time_s` 含等候和步行，`detour_time_min` 保留符号。 |
| `flight_usage.csv` | 每个已实例化班次一行。固定航线、OD、起降秒、容量、已订/剩余席位、按请求 ID 的订座人数与状态；末批展望期可含尚未起飞的空航班。利用率以已起飞班次容量为分母。 |
| `performance.json` | 各计时项、候选、修复、缓存、车辆状态等分布及计数。分位数含中位、p95、p99；汇报 M4 与整批超时比例和最大超时量。内存字段是容器浅层字节数，事件缓冲还包含事件对象浅层大小；同时记录请求、任务、航班数量与队列长度。 |
| `audit.json` | 重新流读压缩事件，核对到达唯一性、执行时刻、任务链、派车和容量、道路连续性、全部车辆收尾、座位与起降、逐批与最终数量、评价独立分母。`road_metres_by_vehicle` 按车及行驶用途统计。 |
| `summary.json`、`summary.md` | JSON 汇总分期、评价 SR、服务模式、等待/完整出行/有符号绕行、小时及每日评价、席位利用、里程、性能与审计结论；Markdown 给出主指标、模式及性能表。空分母为 JSON null。 |
| `failure.json`、`errors/` | 非零退出时记录异常、批次、最后成功批次、快照及栈。P1/M4/M5 的专项诊断在触发时写入 `errors/`。初始化前的错误可能没有世界快照。 |

## 7. 墙钟预算与CPU诊断

- 正式Wall Stop：`time.perf_counter()`；另记`time.process_time()`。
- 用户手动短窗口CPU预算对比：CPU Stop改成`process_time()`，同时设置独立额外Wall安全上限。
- 无论停止口径如何都记录真实Wall耗时；不能以CPU预算10秒却Wall消耗13秒的案例宣称满足论文10秒批次。
- M4 搜索计时截止于 `SearchBudget.finish()`；另有含可选终检的 M4 调用耗时。M3/M5、验证器、事件推进均计入整批时长。
- 超时检查协作式，可能少量越界，必须报告超时批次和最大幅度。

## 8. 验收状态机

`RUNNING -> COMPLETE`；
异常分支：`RUNNING -> FAILED`，用户停止：`RUNNING -> INTERRUPTED`。
不可自动把FAILED当作“部分成功”合并进入统计均值。
