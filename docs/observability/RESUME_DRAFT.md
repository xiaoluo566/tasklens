# TaskLens 简历成稿

## 项目经历

### TaskLens

个人项目

2026.08 - 至今

**技术栈：** Python / FastAPI / Pydantic v2 / CDP / SQLite / Pytest / Playwright

**项目介绍：** TaskLens 是一个面向复杂网页流程的单 Agent AI 浏览器测试平台，支持自然语言任务执行、结果校验、失败归因与步骤回放，将“任务理解 → 页面观察 → 动作执行 → 结果验证 → 终态判定”的黑盒过程转化为可观察、可解释、可追溯的测试证据。

- 围绕真实浏览器任务的单 Agent 执行链路，搭建从 `TaskSpec`、Observation、Action 到 Assertion、`AgentResult` 的状态链，引入最大步数、任务级超时和幂等重试约束，将自然语言目标收敛为可控执行流程，解决 Agent 状态分散、异常循环和终止条件不清的问题。

- 基于 `browser-harness` 的 CDP/session 与浏览器 helper 能力，设计统一的 Browser Adapter、动作协议和错误映射层，将 Agent 决策与底层浏览器生命周期解耦，解决环境差异导致的接口不一致、连接故障难定位和依赖难替换的问题。

- 引入文本、元素状态和结构化结果三类断言，结合 helper trace、DOM/状态快照、首次偏离步骤与 `failure_kind`，建立动作结果、页面证据和业务终态的分层判定链，避免将 helper 成功误判为业务成功，并支持模型、环境和动作失败归因。

- 通过执行追踪与本地优先约束，建立 Pydantic v2 + SQLite 的任务—步骤—断言证据链，配套 FastAPI 查询、敏感字段脱敏、长度限制和 fail-open 降级，解决日志分散、历史不可检索和观测故障影响主任务的问题；已用 Pytest/Pyright 验证核心链路，Playwright 真实浏览器流程待目标环境补证。

## 精简版

简历空间不足时保留下面三条：

- 围绕单 Agent Runtime、Browser Adapter、Evidence Store 与 Dashboard，形成任务理解、页面观察、动作执行、结果校验和失败复盘闭环。
- 封装 `browser-harness` 与 CDP 的浏览器会话、页面状态及点击/输入/等待等动作能力，通过任务超时、最大步数、幂等重试和业务断言控制执行风险。
- 使用 Pydantic v2 + SQLite 沉淀脱敏运行证据，通过 FastAPI + Dashboard 展示运行历史、失败步骤和耗时，并以 Pytest/Playwright 验证关键链路。

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
