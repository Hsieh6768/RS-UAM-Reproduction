# 发送给 Codex 的第一条任务指令（可直接复制）

请以当前 `RS-UAM-Reproduction` 文件夹作为**唯一**工作区。

1. 依次阅读 `AGENTS.md`、`README.md`、`docs/DECISIONS.md`、`docs/PAPER_TRACEABILITY.md`、
   `docs/ARCHITECTURE.md`、`docs/DATA_AND_PROTOCOL.md`、`docs/TEST_AND_GATES.md` 当前阶段条款、
   `docs/EXPERIMENT_COMMANDS.md`、`docs/OPEN_ITEMS.md`、本文件及 `codex_tasks/P0.md`。
2. 仅执行 P0。先核对现有 Conda `ridesharing` 解释器路径、Python **3.9.25** 和依赖版本。依赖变动须等用户确认。
3. 审计根目录的 `new-york-190101.osm.pbf`、`nyc_2013_05_04_to_05_08.csv` 和我已批准的
   `air_routes_manhattan.json`、`vertiports_manhattan.json`。用根目录的固定拓扑，保留原方向。
4. 输出 P0 来源和清洗账本、可复现配置与轻量测试。复制已确认文件到 `data/raw/` 后复核哈希，保留根目录原件。
5. **禁止**运行真实数据的10/30分钟、5小时或48小时仿真实验，禁止后台长任务，
   禁止自行进入P1。
6. 完成后给出精确改动清单、已执行测试命令/结果、P0 Gate结论、缺口和下一阶段建议。

如果论文与已确认决策冲突，请提供原文页码与最小反例，不要自行改动模型。
