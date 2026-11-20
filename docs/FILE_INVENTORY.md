# Codex 交接清单与本地输入

## 已包含（随 ZIP）

- `AGENTS.md`, `README.md`：工作纪律与入口。
- `docs/DECISIONS.md`：已确认 D001–D015。
- `docs/PAPER_TRACEABILITY.md`：论文§/公式/表格依据及推断/工程补充区别。
- `docs/ARCHITECTURE.md`：`src/m1`–`src/m5`、事件引擎、数据对象与接口。
- `docs/DATA_AND_PROTOCOL.md`：TLC、OSM、5%分层抽样、预热、评价、尾部延续。
- `docs/TEST_AND_GATES.md`：完整验收和失败策略。
- `docs/EXPERIMENT_COMMANDS.md`：每个实验的**可复制命令**和权限。
- `docs/RUN_OUTPUTS.md`：控制台/CSV/JSON/日志/异常快照协议。
- `docs/OPEN_ITEMS.md`：尚需数据审计或小例核验的不可自行决定项。
- `codex_tasks/START_WITH_P0.md`：第一条粘贴给Codex的完整任务。
- `codex_tasks/P0.md`–`P7.md`（P4为P4A/P4B）：分阶段任务卡。
- `configs/*.yaml`：论文默认/10min/30min/5h/48h/CPU诊断/大型模板。
- `references/paper.pdf`：用户提供的原论文完整PDF。
- `src/*/__init__.py`：按模块组织的 Python 包。P0 离线入口和模块见 `README.md`。
- `tests/*/.gitkeep`：测试目录框架。P0 人工用例位于 `tests/unit/`。
- `pyproject.toml`, `.gitignore`：Python项目基础与数据忽略规则。

## 当前工作区本地输入与环境

1. **TLC 原始 CSV：** `nyc_2013_05_04_to_05_08.csv`。
2. **OSM 原始快照：** `new-york-190101.osm.pbf`。
3. **固定航空输入：** `air_routes_manhattan.json` 与 `vertiports_manhattan.json`。四份原件保留在根目录，副本见 `data/raw/`。
4. **运行环境：** 现有 Conda `ridesharing` / Python **3.9.25**。Gurobi 求解许可待需要求解的阶段验证。
5. **阶段实现：** P0 提供离线预处理，调度和仿真按后续阶段推进。

## 质量保证

历史交接包的清单见 `handoff_manifest.json`。当前数据原件与归档 SHA256 记录于
`data/source_manifest.json`，P0 处理产物的 SHA256 记录于各自的 `manifest.json`。
