# 控制台输出、实验文件和性能统计契约

## 1. 控制台：每批一条可读摘要

目标格式（数据纯示例，不是实测）：
```
[2013-05-05 08:30:10][Batch 3782][EVAL] new=5 pending=19 accepted+=3 expired+=1 |
fleet idle=12 busy=34 rebalance=4 | M4 wall=243.5ms cpu=239.8ms |
candidates=450 components=7 best=827.40 fallback=no | batch=262.1ms
```
- 所有真实数据10/30分钟、5小时和48小时实验由用户手动启动；用户可实时观察每批输出。
- 默认每批一条摘要；每60批追加累计指标小计；异常单独打印ERROR并同步日志。
- 大规模逐候选日志禁止直接逐行输出；保存错误批次完整诊断而不是刷屏。
- Ctrl+C时尽可能写入`INTERRUPTED`标志与已完成统计，不把部分结果伪装成完整论文运行。

## 2. 每次独立输出目录

`outputs/<run_id>/`至少包括：
```
manifest.json
config_resolved.yaml
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

## 7. 墙钟预算与CPU诊断

- 正式Wall Stop：`time.perf_counter()`；另记`time.process_time()`。
- 用户手动短窗口CPU预算对比：CPU Stop改成`process_time()`，同时设置独立额外Wall安全上限。
- 无论停止口径如何都记录真实Wall耗时；不能以CPU预算10秒却Wall消耗13秒的案例宣称满足论文10秒批次。
- M4总函数入口到返回计M4时长；M3/M5/验证器/事件推进计整批时长但非M4。
- 超时检查协作式，可能少量越界，必须报告超时批次和最大幅度。

## 8. 验收状态机

`CREATED -> RUNNING -> COMPLETE`；
异常分支：`RUNNING -> FAILED`，用户停止：`RUNNING -> INTERRUPTED`。
不可自动把FAILED当作“部分成功”合并进入统计均值。
