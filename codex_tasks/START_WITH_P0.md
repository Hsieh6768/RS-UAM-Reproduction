# 发送给 Codex 的第一条任务指令（可直接复制）

请以当前 `RS-UAM-Reproduction` 文件夹作为**唯一**工作区。

1. 先逐字阅读 `AGENTS.md`、`README.md`、`docs/DECISIONS.md`、
   `docs/PAPER_TRACEABILITY.md`、`docs/ARCHITECTURE.md`、`docs/DATA_AND_PROTOCOL.md`
   和 `docs/TEST_AND_GATES.md` 的P0部分。
2. 仅执行 `codex_tasks/P0.md`，先审计由我放在根目录的 OSM 原始文件和
   `nyc_2013_05_04_to_05_08.csv`，检查真实格式、文件哈希和输入异常。
3. 检查是否能只读找到我已批准的**旧工程固定航空航线/站点静态配置**，
   预期文件名可能为 `air_routes_manhattan_fig7a_approx.json`；
   不要使用或复制旧工程的任何调度代码。找不到时明确告诉我需要哪个数据文件。
4. 输出P0的数据来源和清洗账本、可复现配置与轻量测试；已确认文件归档到 `data/raw/`，
   不覆盖原始文件或处理过的样本。
5. **禁止**运行真实数据的10/30分钟、5小时或48小时仿真实验，禁止后台长任务，
   禁止自行进入P1。
6. 完成后给出精确改动清单、已执行测试命令/结果、P0 Gate结论、缺口和下一阶段建议。

如果论文与已确认决策冲突，请提供原文页码与最小反例，不要自行改动模型。
