# P6 真实负载性能诊断实施报告

**READY_FOR_MANUAL_PROFILING**。诊断配置继承现有 E10，新增一个有上限的采样文件。固定工作量和人工时钟对照验证了开关两侧的 HALNS 抽签、RNG 状态、资源绑定、任务顺序与最终调度。完整轻量回归 **309 passed in 9.48s**，包括原有 291 项及新增 18 项。真实负载的阶段占比和诊断开销仍待用户手动运行后测量。

## 手动启动与读取

在工程根目录打开 PyCharm Terminal，使用现有环境：

```powershell
conda activate ridesharing
python -u -m src.cli.main simulate --config configs/debug_10min_profile.yaml --fleet-size 50 --variant rs_uam --seed 101
```

已验证同一命令附加 `--dry-run` 的结果为 PROFILING、50 车、7200 秒预热、600 秒评价、10 秒 M4 墙钟预算、独立校验开启。本轮运行了合成测试和元数据预检。E10/E11 的真实时间窗留待用户启动。

使用终端打印的独立 run 目录。先检查 manifest 的 `status`、`run_class` 与 `profiling_counts`，完整运行再检查 `audit.json`。按批号关联 `profiling.jsonl` 与 `batches.csv`。例如，以下命令读取人工指定运行中的首个采样批，只展示阶段的自身耗时：

```powershell
$run = Read-Host '输入终端打印的本次 run 目录'
$row = Get-Content -LiteralPath (Join-Path $run 'profiling.jsonl') -Encoding UTF8 -TotalCount 1 | ConvertFrom-Json
$row.stages.PSObject.Properties | Select-Object Name,@{N='calls';E={$_.Value.calls}},@{N='exclusive_ms';E={$_.Value.exclusive_ns / 1e6}}
$row.requests | Select-Object request_id,plan_count,has_binding_options,attempts,completed_attempts,first_conflict,final_state,outcome
```

## 抽样、容量与预算

| 配置键 | E10 诊断值 | 口径 |
|---|---:|---|
| enabled | true | 其他实验未配置时默认 false，批次 CSV、控制台摘要及结果文件契约沿用原样 |
| batch_every | 10 | 第 1、11、21…批。周期为整数，无 RNG 调用 |
| candidate_every | 32 | 在选中批内从 S1 编号 1 开始，详细计时第 1、33、65…个已开始的候选 |
| max_candidates_per_batch | 8 | 每个采样批最多 8 个详细候选。七个算子另外保证各自首次生成被计时；所有实际生成调用仍计数 |
| max_detail_spans_per_batch | 512 | 热路径的构图、物理解码、冲突选择、删除、重建、合并等子调用共用上限。达到上限即直接执行原调用 |
| max_requests_per_batch | 3 | 对本批进入规划目录的 Type1/2，用 BLAKE2b-64 对“批号/请求 ID”的 UTF-8 字节排序，哈希相同则按 ID 排序，选取最小的至多 3 条。允许设为 0 |
| max_output_bytes | 4194304 | 全程详细诊断文件硬上限为 4 MiB，按实际 UTF-8 字节（含换行）检查。首行也可能容不下。其他原有实验文件各自沿用既有规则 |

每个采样批在内存中聚合。候选和修复循环没有文件 I/O，也不保存逐修复、逐候选轨迹。每批最多写 1 行，行内最多 3 条请求，各自只保留现有 M1 方案及正式腿的计数。按默认配置，单个采样批至多 512 个热路径子计时，加上候选包络、首次算子、S0、终检与事务等，合计至多 **543 个计时间隔**，每个间隔读专用时钟两次。另有记录构造和写盘的一对时钟读取。工作量仍随实际候选数、选中请求数及活跃目录大小变化；计时间隔上限不是对执行毫秒数的保证。

行内 `candidate_samples_truncated` 统计按步长本应选择、因候选上限被排除的数量，`detail_spans_truncated` 统计因子调用上限省略的计时数，`requests_truncated` 为等待目录数减去选中数。步长主动跳过的候选与达到上限的候选分别解释。下一完整行若会超过总字节数，整行不写，后续采样批也停止详细观测。manifest 的 `batches_truncated` 统计因此省略的采样批，`first_truncated_batch` 标识首批。正常完成以及程序处理的失败/中断都会保存汇总；操作系统强制结束进程时不能保证最终 manifest 已更新。

预算继续使用原有 SearchBudget 的墙钟/CPU 和检查点。诊断使用单独的 `perf_counter_ns`，没有调用 HALNS RNG，也不借用注入的预算时钟。请求被尝试、候选完成、修复、评分及退火的原有判定未变。真实墙钟持续前进，观测的开销可能改变截止前的候选数及后续随机轨迹。PROFILING 运行适用于成本定位，逐批服务率一致性只能在固定工作量、人工时钟测试中检查。

## 阶段计时的解释

每个 `stages` 条目保存 `calls`、`inclusive_ns` 和 `exclusive_ns`。计时栈在退出时从父阶段扣除**直接子阶段**的完整耗时。只对同一覆盖范围的 exclusive 值求和。未采样或已截断的子调用仍实际运行，其耗时留在测得的父阶段自身时间中。输出中没有某个阶段，表示该批没有记录这种调用。没有运行时钟插值或外推到全部候选。

| 阶段 | 实际边界 |
|---|---|
| request_sampling | 挑选请求、提取 M1/M3 元数据。发生在 M4 入口前，属于诊断开销 |
| inherit_sequence、s0_setup | 从前批恢复剩余顺序、构造 FullDecoder。s0_setup 含 s0_prepare（固定承诺/基础编码）及 s0_decode（原约定时刻完整回放） |
| s1_build、s1_decode | 随机初解编码与实际解码；S1 始终是选中批的第一个详细候选，前提是预算允许启动 |
| operator_scope_graph、operator_各算子名 | 前者含生成作用域所需的任务解析、建图及弱连通识别；后者仅量实际编码生成。operator_calls 是该批七算子各自执行生成的次数，和外层轮盘抽签次数不同 |
| candidate_decode、task_graph | 后续被采候选的解码包络；task_graph 含初始建图及弱连通分量识别 |
| local_physical、select_conflict、repair_remove | 分量缓存未命中时的物理计算（包括产生冲突的检查），多个冲突之间按原规则选择首个冲突，以及删除对应普通请求。select_conflict 仅量冲突选择 |
| repair_graph、repair_local_physical、combine | 删除后重建受影响图、该解码在至少一次删除后的本地物理重算、将成功分量合并为完整解。缓存命中没有物理重算 |
| full_decode、repair_full_decode | noD 完整解码器中首次尝试与删除后的完整尝试；沿用原模式开关 |
| validator、passenger_commit、rebalance_commit | 原有最终独立 ScheduleValidator、M4 事务、M5 发布或跳过处理各调用一次。均在 M4 预算外，独立验证关闭时没有 validator 条目 |

`candidates_started/evaluated` 直接来自原 SearchStats。候选开始生成、实际进入物理解码、通过预算检查计为完成是不同的边界。计时器在异常退出时也结束已开启的阶段，故耗时可以包含末次未完成尝试。已有 `m4_wall_ms`、`m4_call_wall_ms`、`validator_wall_ms` 与 `batch_wall_ms` 仍是各自原有的实测包络。不要将诊断的 inclusive 值再加到这些字段。选中请求的尝试/完成取证发生在被测候选解码调用外，真实搜索墙钟仍包含这部分观测。逐批诊断序列化、写盘发生在取整批时间之后，manifest 的 `record_and_write_wall_ns` 单列该包络。

## 请求与 M5 证据

对于每个抽中请求，保存 `original_second`，M1 的 `plan_count` 及 plan_id，各正式段的 leg_id、mode 和 M3 实际返回数量，单方案与全请求的资源覆盖标记。`has_binding_options` 表示至少一个有正式段的方案在每个正式段上均能选到资源，尚未验证联合时序、容量或物理可达性。

请求证据覆盖采样批内**所有进入解码器的候选**，独立于候选计时步长。`attempts` 要求请求出现在输入编码；`completed_attempts` 要求这次解码通过原有预算检查点。`first_conflict` 是已完成解码中首次归于该请求的删除或终止冲突；未完成候选的部分内部状态不作推断。`retained_feasible` 计实际留在完整可行试解中的次数。`removed_on_conflict` 计在已完成解码中从输入编码删去该请求的次数。`final_state` 读自真实提交后的快照。

`outcome` 按证据优先级标记为已接受、曾有合法试解但未被最终选择、存在中断尝试、已观察到删除、参与的试解不可行、无完整逐段资源选项、未被尝试（分别为 accepted、feasible_trial_not_selected、interrupted_attempt、removed_in_observed_trials、trial_infeasible、no_complete_binding_options、not_attempted）。预算停止另见同一行的 `budget_exhausted/stop_reason`。某批用尽预算不能单独解释某条请求为何未接受，本轮未添加反事实解码。

M5 只读取既有 RebalancePlan 和真实提交返回的移动数。`cross_quota` 为 OPTIMAL 流的跨格配额，`published` 为实际发布数，`first_blocked_source` 与 `matched_at_blocked_source` 分别来自首个阻塞源格及该源格在失败前的已匹配槽位。后者不代表全批的最大可执行数。格流、可达详情、solver_status 仍可和 batches.csv 对照。固定流最大匹配继续留在原离线工具 `analysis/p6_real10/m5.py`，本轮没有重跑历史 1054 批审计。

## 合成对照与回归

使用 ridesharing 的 `D:/Programs_D/Anaconda/envs/ridesharing/python.exe`，Python 3.9.25、pytest 8.4.2。三节点、1 车、8 请求，固定种子及算法时钟，每次都完成 33 个候选。对照只改接客窗口，每种窗口交替开关诊断，完成 7 对 solve，取中位数。外部墙钟包含搜索和原有最终验证；内部阶段表是选中候选的观测值。测试入口为 `python -B -m tests.performance.p6_profiling_synthetic`。

| 合成窗口 | pickup_window 修复/次 | solve 中位数，关 / 开（ms） | 首次局部物理 / 修复图重建 / 修复后局部物理的采样 exclusive 中位数（ms） |
|---|---:|---|---|
| 宽松，LP=420 | 0 | 19.474 / 20.075 | 0.232 / 未触发 / 未触发 |
| 紧张，LP=10 | 40 | 4.805 / 5.144 | 0.040 / 0.197 / 0.157 |

该次采样批的 solve 增量约 0.601 ms（3.1%）和 0.339 ms（7.1%）。紧窗口删除请求后留下的任务较少，因此两个夹具之间的总时间不能直接解释为修复净成本。默认只测每 10 批中的 1 批。若真实开销明显，优先增大 batch_every/candidate_every，或降低子计时及请求上限；这样会降低观测覆盖率，搜索预算仍采用原值。

`python -B -m pytest -p no:cacheprovider -q` 得到 **309 passed in 9.48s**。原有轻量回归全部保留。新增测试覆盖配置继承与非法界限、默认文件契约、嵌套计时人工 oracle、RNG/编码/最佳解与候选工作量、两种解码器的接客冲突、采样/字节上限和校验和、真实事件 BBAA→BAA 及资源绑定、超时保留已经完成的优解、M5 整体拒绝、最终校验故障快照及非零退出。

## 改动清单与适用范围

| 文件 | 改动 |
|---|---|
| configs/debug_10min_profile.yaml | 继承 debug_10min.yaml，仅增加 profiling 节 |
| src/evaluation/profiling.py | 配置校验、稳定采样、阶段计时栈、请求证据及按批限额写出 |
| src/cli/experiment.py、src/evaluation/outputs.py | 配置解析、有效配置、PROFILING 标记、唯一目录、完成与失败清单 |
| src/simulation/rolling.py | 在 M3 后采样，观察原有 M4/M5 提交，按批写出 |
| src/m4/halns.py、decoder.py、partitioned_decoder.py（均在 src/m4/） | 可选 observer、初始化/生成/解码/修复/终检的原调用边界 |
| tests/integration/test_p6_profiling.py、tests/integration/test_p6_rolling.py | 新检查及复用现有滚动夹具的可选诊断参数 |
| tests/performance/p6_profiling_synthetic.py | 固定规模的人工性能对照 |
| docs/EXPERIMENT_COMMANDS.md、docs/RUN_OUTPUTS.md、本报告 | 手动命令、字段和复核依据 |

实现沿用 D004–D007、D010、D014 的事件、解码、修复、搜索、再平衡和预算规则。已有 E10 的 86.34% M4 占比和 62.56 次修复/候选来自原诊断。仍缺少真实 E10 各函数的实际耗时分布、未完成候选内部冲突序列、各采样请求未尝试绑定的可行性以及改变模型的服务收益。这些都不能由本次合成数据推出。
