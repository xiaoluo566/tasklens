# TaskLens 简历成稿

## 项目经历

### TaskLens

个人项目

2026.08 - 至今

**技术栈：** Python / FastAPI / Pydantic v2 / CDP / SQLite / Pytest / Playwright

**项目介绍：** TaskLens 是一个面向复杂网页流程的单 Agent AI 浏览器测试平台，支持自然语言任务执行、结果校验、失败归因与步骤回放，将“任务理解 → 页面观察 → 动作执行 → 结果验证 → 终态判定”的黑盒过程转化为可观察、可解释、可追溯的测试证据。

- 基于 `browser-harness` 的 stdin 执行模型与 helper trace，设计并实现单 Agent Runtime，以 `TaskSpec` 约束目标、动作、最大步数、任务级超时和幂等重试，在 Observation→Action→Assertion 循环中产出 `AgentResult`，解决自然语言任务执行状态不可控、异常循环和终止条件不清的问题。

- 基于 daemon 的 CDP WebSocket、Tab/session 管理和 helper 能力，设计并实现 Browser Adapter，统一页面观察、动作调用与错误映射，隔离 Agent 决策和浏览器生命周期，解决不同浏览器环境下接口不一致、连接故障难定位和底层依赖难替换的问题。

- 基于 helper trace 与页面 DOM/状态快照，设计并实现文本、元素状态、结构化结果三类断言及证据模型，记录首次偏离步骤和 `failure_kind`，解决“helper 调用成功但业务结果错误”以及模型、环境、动作失败难区分的问题。

- 基于 `run.py` helper trace、recorder 事件和本地优先约束，设计并实现 Pydantic v2 + SQLite 运行证据层及 FastAPI 查询接口，加入敏感字段脱敏、长度限制和 fail-open 降级，解决日志分散、历史不可检索和观测故障影响主任务的问题；通过 Pytest、Pyright 与 Playwright 验证状态流转、接口契约和真实浏览器流程。

## 精简版

简历空间不足时保留下面三条：

- 构建单 Agent Runtime、Browser Adapter、Evidence Store 与 Dashboard，形成任务理解、页面观察、动作执行、结果校验和失败复盘闭环。
- 基于 `browser-harness` 与 CDP 封装浏览器会话、页面状态及点击/输入/等待等动作能力，通过任务超时、最大步数、幂等重试和业务断言控制执行风险。
- 使用 Pydantic v2 + SQLite 沉淀脱敏运行证据，基于 FastAPI + Dashboard 展示运行历史、失败步骤和耗时，并通过 Pytest/Playwright 验证关键链路。

## 与开发文档的对应关系

| 简历内容 | 对应开发契约 | 实现证据 |
| --- | --- | --- |
| 单 Agent 执行闭环 | `DEVELOPMENT_SPEC.md` 的 Runtime 生命周期 | `agent_runtime.py` 及状态机测试 |
| Browser Adapter / CDP | `ARCHITECTURE.md` 的模块边界 | `browser_adapter.py` 及连接/动作测试 |
| 业务结果校验 | `REQUIREMENTS.md` 的断言需求 | 文本、元素状态、结构化结果断言测试 |
| Pydantic + SQLite | `DEVELOPMENT_SPEC.md` 的数据契约 | 模型、schema、repository 及事务测试 |
| FastAPI + Dashboard | `PRD.md` 的用户流程与 API 契约 | API 集成测试、页面截图、Playwright 报告 |
| 故障归因与 fail-open | `ARCHITECTURE.md` 的失败语义 | environment/connection/action/assertion/timeout/model 测试 |

## 项目边界

TaskLens 在简历中统一按个人项目表述，技术依赖写为“基于 `browser-harness` 与 CDP 构建浏览器操作层”。面试时需要准确说明：开源依赖提供浏览器连接和基础 helper，个人负责单 Agent Runtime、任务契约、断言、证据存储、API、Dashboard 与测试体系。
