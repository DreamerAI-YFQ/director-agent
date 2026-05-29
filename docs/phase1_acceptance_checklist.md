# Phase 1 验收 Checklist

本文档把项目书中的 Phase 1 五个核心模块映射到当前代码、数据库表、接口和可验证命令。状态定义：

- Done：已有可运行实现和基本测试/命令可验证。
- Partial：主干已实现，但还缺验收所需的边界、测试或运维能力。
- Missing：当前代码中尚未形成可验收能力。

## 1. 数据接入

状态：Partial

验收标准：
- 第三方视频/广告数据可进入 staging 表，并保留原始字段。
- 字段级校验、质量标记、hash 去重、失败原因可追踪。
- 合格数据可触发或关联 P0-P5 Pipeline。

当前证据：
- 数据表：`staged_videos`，含 `video_hash`、`raw_data`、`quality_flag`、`validation_issues`、`pipeline_run_id`。
- 脚本：`scripts/data_ingestion_demo.py`。
- 迁移：`migrations/001_initial_schema.sql`。

缺口：
- 真实第三方 API 接入和视频下载重试仍是 demo 级。
- 字段 schema 版本、CDN 失效 fallback、批量导入监控还未完整工程化。

验收命令/接口：
- `python scripts/init_db.py --status`
- 检查 `staged_videos` 是否完成去重和质量标记。

## 2. Prompt 编排引擎

状态：Partial

验收标准：
- P0-P5 按 DAG 串/并行调度：P0 -> P1/P2 -> P3 -> P4 -> P5。
- Prompt 版本、schema 版本、输出 schema 可绑定和追踪。
- 跨 Prompt element_id 引用可入库并支持反查。
- Provider 失败重试、fallback、成本记录可用。

当前证据：
- 调度器：`agent/pipeline.py`。
- Prompt 版本：`agent/prompt_manager.py`、`prompts/manifest.json`。
- 输出校验：`agent/pipeline_schema.py`，每阶段结果写入 `_schema_validation`。
- 引用追踪：`agent/reference_tracker.py`，支持 `META_001/RPT_001` 这类 key-as-element-id。
- 数据表：`pipeline_runs`、`pipeline_stages`、`prompt_references`、`cost_records`。
- 测试：`tests/test_pipeline_schema.py`、`tests/test_reference_tracker.py`。

缺口：
- Schema 失败当前只记录，不会 fail-fast 或进入人工处理队列。
- Prompt schema 兼容矩阵已有雏形，但真实迁移策略仍需产品/客户共同确认。
- 成本告警已有记录能力，缺少后台通知渠道。

验收命令/接口：
- `python -m pytest tests/test_pipeline_schema.py tests/test_reference_tracker.py -q`
- `GET /api/pipeline/runs`
- `GET /api/references/{element_id}`
- `GET /api/pipeline/cost`

## 3. 词典管理系统

状态：Partial

验收标准：
- 9 套词典可增改查。
- 词典版本历史、Diff、当前版本可查询。
- 合规检查使用当前词典版本并保存检查记录。

当前证据：
- 数据表：`dictionaries`、`dictionary_versions`、`compliance_checks`。
- 存储接口：`agent/storage.py` 中 `dict_upsert`、`dict_search`、`dict_diff`、`dict_get_current_version`。
- API：`GET /api/dict/{dict_type}`、`GET /api/dict/{dict_type}/versions`、`GET /api/dict/{dict_type}/diff/{key}`。
- Agent 工具：`search_dictionary`、`check_compliance`。

缺口：
- Prompt 嵌入式标签库的可编辑与发布流程还没有完整后台。
- 词典版本与历史 Pipeline 查询的“按时间点解释旧数据”能力还需要补查询层。

验收命令/接口：
- `GET /api/dict/monitor`
- `GET /api/compliance/history`

## 4. 候选标签 Review 队列

状态：Partial

验收标准：
- P3/P5 中 novel 标签可捕获、跨视频去重、累计频次和置信度。
- Review 队列支持批量 approve/reject/merge。
- approve 后可进入对应词典，形成 Novel -> Review -> Dictionary 的闭环。

当前证据：
- 模块：`agent/novel_capture.py`。
- 数据表：`novel_tag_candidates`。
- API：`GET /api/review/candidates`、`POST /api/review/decide`、`GET /api/review/stats`。
- Pipeline 完成后会调用 `on_pipeline_complete` 捕获候选标签。

缺口：
- Review 决策后的老数据回标、事务边界和审计日志还不够完整。
- merge 语义和客户 SOP 的具体字段还需要确认。

验收命令/接口：
- `GET /api/review/candidates`
- `POST /api/review/decide`

## 5. 极简管理后台

状态：Partial

验收标准：
- 可查看任务、阶段状态、错误、成本。
- 可查看词典、Review 队列、历史对话。
- 关键失败可重试。

当前证据：
- 前端：`static/index.html`。
- 服务入口：`main.py`。
- 对话进度条已改为跟随后端 `progress/state`。
- API 覆盖 Pipeline、Reference、Review、Feedback、RAG、Dict、Compliance、Sessions。

缺口：
- 目前更像单页调试台，不是完整后台信息架构。
- 错误重试只覆盖部分引用/陈旧运行场景，Pipeline 阶段级人工重跑还需要补。
- 前端自动化回归和浏览器验收测试还不足。

验收命令/接口：
- `python main.py`
- 打开 `http://127.0.0.1:3000`
- `python -m pytest -q`

## 总体判断

当前 Phase 1 更接近“工程原型可演示 + 核心架构主干已成型”，还不是“可交付验收版”。建议验收前必须补齐：

- 数据接入从 demo 变成真实 API/下载/脏数据隔离链路。
- Pipeline schema 失败后的处理策略。
- Prompt schema 迁移与历史数据解释查询。
- Review 队列的审计、事务和回标。
- 管理后台的任务重试、成本告警和基础前端回归测试。
