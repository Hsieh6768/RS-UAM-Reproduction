# RS-UAM-Reproduction

**定位：** 复现 Li 等发表于 *Transportation Research Part C* 175 (2025), 105111 的
*On-demand ridesharing based on dynamic scheduling in urban air mobility*。
本仓库是 **Codex 开发工作区交接包（设计/任务/配置），不是已完成的运行程序**。
代码、CLI、测试、运行报表需要 Codex 按阶段实现；文档中出现的命令属于目标接口，当前不得承诺能直接运行。

## 首次使用

1. 创建或解压为 `RS-UAM-Reproduction/` 项目目录。
2. 将 OSM 原始道路网络文件、`nyc_2013_05_04_to_05_08.csv` 和 `air_routes_manhattan.json` 放在项目根目录。三个文件由 P0 统一审计；除输入位置外，不要求用户预先整理。
3. 将仓库作为 Codex 的工作区，先阅读 `AGENTS.md`、`docs/DECISIONS.md`、`docs/PAPER_TRACEABILITY.md`。
4. 向 Codex 发送 `codex_tasks/START_WITH_P0.md` 的内容，并仅授予 P0 开发范围。
5. Codex 在 P0 中先盘点根目录的原始文件（**不改动字节/不覆盖/不猜格式**），再在 `data/raw/` 归档，输出来源哈希和筛选数量账本。
6. Codex 每完成一阶段报告测试证据；用户决定是否进入下一阶段。
7. **除轻量单元测试外，所有真实数据仿真由用户在控制台手动启动**。请参阅 `docs/EXPERIMENT_COMMANDS.md`。
8. 本项目的唯一运行环境为现有 Conda 环境 ridesharing。所有开发、测试及仿真必须先确认使用该环境；具体命令见 docs/EXPERIMENT_COMMANDS.md。

## 仓库导航

| 文件 | 用途 |
|---|---|
| `AGENTS.md` | Codex 必须遵守的工程与权限边界 |
| `docs/DECISIONS.md` | D001–D015 已确认的详细决策（取代旧 v0.1 草案） |
| `docs/PAPER_TRACEABILITY.md` | 论文依据、明确规定/工程推断/未公开内容 |
| `docs/ARCHITECTURE.md` | src 分模块目录、对象、接口、事件/提交语义 |
| `docs/DATA_AND_PROTOCOL.md` | 2013 数据清洗、抽样、预热、尾部延续、统计 |
| `docs/TEST_AND_GATES.md` | 模块验收、独立对照测试和放行条件 |
| `docs/EXPERIMENT_COMMANDS.md` | **专用命令手册：命令、实验目的、运行权限** |
| `docs/RUN_OUTPUTS.md` | 控制台输出、结果文件、审计与性能统计格式 |
| `docs/OPEN_ITEMS.md` | 缺失数据与论文歧义的处理规则 |
| `codex_tasks/P0.md`–`P7.md` | 每阶段 Codex 任务与停止条件 |
| `configs/*.yaml` | 待实现的配置合同；不能代替代码中的验证 |
| `references/paper.pdf` | 用户提供的原始论文，供 Codex 精确核对 |
| `src/` | 只存新写的源码，按 M1–M5 等子目录组织 |
| `tests/` | 独立对照、单元、集成、回归及性能测试 |

## 不可违反的边界

- 旧工程**仅允许审计并复用批准的静态航空拓扑/站点配置**。不能直接复制旧版优化器、模拟器或数据处理代码。
- 论文原文若与本交接包有冲突，**先报告差异，不自行更改已确认的方案**。
- 所有正式实验使用 10 秒模拟调度周期，**M4 时间预算为墙钟 10s/30s 可配置**，模拟决策视为即时，不引入 CPU 计算产生的交通延迟。
- 实验服务成功在完整仿真收尾后要求 `accepted == completed`；违反时整次实验失败，不能悄悄输出成功报告。
- 论文指标、用户确认的工程约定、未来可选替代实验应分别标注。

## 源数据与拓扑的重要说明

用户已将以下文件放置于项目根目录：

- OSM 原始道路网络文件（具体名称与格式由 P0 核验）。
- `nyc_2013_05_04_to_05_08.csv`：2013 年 TLC 出租车记录。
- `air_routes_manhattan.json`：拟复用的固定航空航线配置。

上述文件均尚未完成 P0 审计，不代表其格式、地理范围、节点映射及航线数量已经验证通过。

Codex 在 P0 中应当优先检查根目录现有文件，记录原始文件 SHA256、结构及来源，再按规范整理至 `data/raw/`。严禁修改原始文件内容、自动补齐反向航线或自行生成替代拓扑。

大型原始数据和生成结果不提交 Git。已确认的静态航空拓扑建议纳入版本管理。

论文 PDF 位于 `references/paper.pdf`。