# TaskLens 简历成稿

## 项目经历

### TaskLens

个人项目

2026.08 - 至今

**技术栈：** Python / FastAPI / Pydantic v2 / CDP / SQLite / Pytest / Playwright

**项目介绍：** TaskLens 是一个面向复杂网页流程的单 Agent AI 浏览器测试平台，支持自然语言任务执行、结果校验、失败归因与步骤回放，将“任务理解 → 页面观察 → 动作执行 → 结果验证 → 终态判定”的黑盒过程转化为可观察、可解释、可追溯的测试证据。

- 围绕复杂网页任务的单 Agent 执行闭环，搭建从 `TaskSpec`、页面 Observation 到 Action、Assertion、`AgentResult` 的可追溯状态链，将自然语言目标转化为受最大步数、任务级超时和幂等重试约束的浏览器动作，解决 Agent 执行状态分散、终止条件不清的问题。

- 基于 `browser-harness` 与 CDP 协议抽象 Browser Adapter，统一浏览器会话、页面状态提取及点击、输入、滚动、等待、截图等动作接口，将 Agent 决策与底层浏览器生命周期解耦，并区分环境、连接、动作与业务断言失败，提升执行链路的可替换性与故障隔离能力。

- 引入文本、元素状态和结构化结果断言，构建“动作执行结果—页面状态证据—业务终态判定”的分层验证机制，将 helper 调用成功与业务结果正确分开判定，并通过首次偏离步骤和失败类型归因定位环境异常、模型决策错误与页面执行失败。

- 通过 Pydantic v2、SQLite 与 FastAPI 建立从任务、步骤、断言到错误摘要的运行证据链，配合字段脱敏、长度限制和 fail-open 降级策略保证观测异常不影响主任务；使用 Pytest、Ruff、Pyright 与 Playwright 覆盖状态流转、接口契约、异常分支和真实浏览器关键流程。

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
