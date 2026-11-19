# Codex 交接清单与缺失输入

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
- `src/*/__init__.py`：按模块组织的**空Python包，不是实现**。
- `tests/*/.gitkeep`：测试目录框架。
- `pyproject.toml`, `.gitignore`：Python项目基础与数据忽略规则。

## 不包含、用户/环境需要提供

1. **TLC原始CSV：** `nyc_2013_05_04_to_05_08.csv`；用户放在工作区根目录，Codex P0识别归档。
2. **OSM原始路网：** 文件格式和文件名尚未确认；用户放根目录，Codex P0识别格式后决定解析方案。
3. **旧工程固定航空拓扑及起降场静态文件：** 目前未包含。P0允许仅对批准的静态配置作只读查找、校验、复制；找不到应请用户补充，绝不可从图像猜。
4. **可用Python环境与Gurobi许可证：** 用户/本机提供，P0检查现状并记录版本。
5. **完整可运行源码与测试：** **本包是交接和规范文件**，需由Codex逐阶段实现。

## 质量保证

交接清单和源文件SHA256见`handoff_manifest.json`。项目实际运行生成的
`data/source_manifest.json`与`outputs/<run_id>/manifest.json`必须另行由Codex实现，
不得把本交接包哈希当成用户原始数据哈希。
