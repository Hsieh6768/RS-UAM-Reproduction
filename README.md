# RS-UAM-Reproduction

**定位：** 复现 Li 等发表于 *Transportation Research Part C* 175 (2025), 105111 的
*On-demand ridesharing based on dynamic scheduling in urban air mobility*。
仓库按 P0–P7 实施。P0 提供离线输入审计和正式样本，P1 提供有向路由、冻结的批次快照和事件仿真内核。P6 已接通 M1–M5、整批提交及单次实验 CLI，真实短窗待用户手动验收。运行方法见 [实验命令手册](docs/EXPERIMENT_COMMANDS.md)，人工验证证据见 [P6 阶段报告](docs/P6_REPORT.md)。P1 公共接口、事件优先级和验收记录见 [P1 阶段报告](docs/P1_REPORT.md)。

P2 的静态 M1 规划入口为 `M1Planner.generate(request, immutable_network)`。P2 Gate **PASS**。方案资格、D002 时间窗、独立穷举和限量正式请求规划结果见 [P2 阶段报告](docs/P2_REPORT.md)。

## 首次使用

1. 创建或解压为 `RS-UAM-Reproduction/` 项目目录。
2. 本工作区根目录已包含 TLC CSV、OSM PBF、航线 JSON，及补充的站点坐标 JSON。P0 统一审计这些输入。
3. 将仓库作为 Codex 的工作区，先阅读 `AGENTS.md`、`docs/DECISIONS.md`、`docs/PAPER_TRACEABILITY.md`。
4. 向 Codex 发送 `codex_tasks/START_WITH_P0.md` 的内容，并仅授予 P0 开发范围。
5. P0 校验根目录原件并将经哈希复核的副本归档到 `data/raw/`。来源哈希见 `data/source_manifest.json`，本地审计产物见 `data/processed/`。
6. Codex 每完成一阶段报告测试证据；用户决定是否进入下一阶段。
7. **除轻量单元测试外，所有真实数据仿真由用户在控制台手动启动**。请参阅 `docs/EXPERIMENT_COMMANDS.md`。
8. 在 PyCharm Terminal（PowerShell）激活现有 Conda 环境 `ridesharing`，确认解释器为 Python **3.9.25**。环境核对和命令见 `docs/EXPERIMENT_COMMANDS.md`。

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
| `configs/p0.json`、`configs/*.yaml` | P0 审计配置与未来仿真配置契约 |
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

- `new-york-190101.osm.pbf`：OSM PBF 原始快照，含节点、道路 way 和关系，Manhattan 路网从快照中派生。
- `nyc_2013_05_04_to_05_08.csv`：2013 年 TLC 出租车记录。
- `air_routes_manhattan.json`：用户批准的固定有向航线。
- `vertiports_manhattan.json`：用户补充的固定起降场坐标。

根目录原件和归档字节一致。正式输入是完整 Manhattan 有向图、固定航空拓扑与通过有向 OD 可达性筛选的固定样本。图和样本的身份、数量及 SHA256 见 `data/processed/p0_requests/manifest.json`。

大型原始数据和生成结果不提交 Git。已确认的静态航空拓扑建议纳入版本管理。

论文 PDF 位于 `references/paper.pdf`。
