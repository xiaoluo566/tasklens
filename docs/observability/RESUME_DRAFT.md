# TaskLens 简历成稿与开发兑现清单

> 目标岗位：测试开发实习生、AI 测试开发、自动化测试开发。  
> 下面的项目条目按“功能完成态”撰写；后续开发必须按同一份规格实现并补齐测试证据。架构明确采用**单 Agent**，不使用多 Agent 协作。

## 1. 可直接放入简历的项目条目

### TaskLens｜单 Agent AI 浏览器测试与运行观测平台

**技术栈：** Python、FastAPI、Pydantic v2、CDP/WebSocket、SQLite、Pytest、Playwright、Docker

**项目介绍：**

基于开源 `browser-use/browser-harness` 进行二次开发，面向复杂网页流程的单 Agent 自动化测试。平台以自然语言任务为入口，围绕“任务理解 → 页面观察 → 动作执行 → 结果校验 → 失败归因 → 有界重试 → 终态判定”构建执行闭环，并将任务过程沉淀为可脱敏、可查询、可回放的测试证据，解决浏览器 Agent 黑盒执行难定位、难复现和难回归的问题。

**个人工作：**

- 设计单 Agent Runtime 状态机，以 `TaskSpec`、`StepResult` 和 `RunRecord` 统一任务目标、页面观察、浏览器动作、断言结果和最终状态；通过步骤级超时、最大步数和幂等策略控制循环，避免模型在异常页面中无限执行。
- 基于 `browser-harness` 封装 Browser Adapter，复用 daemon、CDP WebSocket、Tab/DOM 感知和点击、输入、滚动、等待、截图等 helper 能力，为 Agent 提供稳定的浏览器操作接口，并隔离浏览器连接异常与业务断言失败。
- 设计并实现本地运行证据层，使用 Pydantic v2 校验 `RunRecord`/`StepRecord`，通过 SQLite 持久化任务、步骤、断言和错误摘要；对 URL 参数、Token、Cookie、Authorization、Password 等敏感数据执行字段级脱敏和长度限制。
- 使用 FastAPI 提供 health、任务运行、运行列表、运行详情和统计接口，设计统一错误响应、状态过滤和分页边界；通过响应式 Dashboard 展示成功率、失败步骤、慢操作和单次任务回放信息。
- 围绕“模型决策—浏览器动作—页面状态—业务结果”关键路径编写 Pytest 单元/集成测试，使用 Ruff、Pyright 做静态检查，并通过 Playwright 验证真实浏览器下的任务执行、失败筛选和证据详情闭环。
- 采用 fail-open 观测策略和分层故障模型：记录或页面服务异常不会改变主任务结果；将环境、连接、动作、断言、超时和模型输出异常分开统计，便于回归定位和问题归因。

## 2. 简历空间不足时的压缩版

**项目介绍：** 基于 `browser-use/browser-harness` 二次开发单 Agent AI 浏览器测试平台，构建任务理解、页面观察、动作执行、结果校验和失败复盘闭环，将浏览器 Agent 的黑盒过程转化为可查询的运行证据。

- 设计单 Agent Runtime 与步骤级状态机，统一 TaskSpec、浏览器动作、断言结果、超时/重试和最终状态。
- 基于 CDP/WebSocket 封装 Browser Adapter，复用 daemon 与 helper 能力；使用 Pydantic + SQLite 沉淀脱敏运行记录，并通过 FastAPI + Dashboard 提供筛选、统计和详情回放。
- 使用 Pytest、Ruff、Pyright 和 Playwright 覆盖执行链路、故障分支、API 契约和真实浏览器关键流程，采用 fail-open 保证观测层异常不阻断主任务。

## 3. 与已有 Skill 评估项目的组合叙事

两个项目分别放在不同层次：

| 项目 | 关注层 | 关键词 |
| --- | --- | --- |
| Skill 评估体系项目 | Skill/Agent 的测试、自证和迭代评估 | 评估指标、测试飞轮、自证飞轮、Harness 设计 |
| TaskLens | 单 Agent 在真实浏览器中的执行、验证和回归证据 | CDP、状态机、断言、脱敏、故障归因、可观测性 |

面试串联：

> 第一个项目回答“如何评价一个 Skill/Agent”，TaskLens 回答“一个单 Agent 在真实浏览器里执行后，如何证明结果正确、定位失败并支持回归”。

## 4. 面试时的关键解释

### 为什么只做单 Agent？

九天周期内，单 Agent 更容易把“观察—行动—校验”闭环做深：状态、证据、重试和失败归因可以统一建模，避免多 Agent 协作带来的通信、调度和结果合并噪声。后续如果需要扩展，多 Agent 只应作为上层编排能力接入，不改变底层 Browser Adapter 和证据契约。

### 和普通 UI 自动化脚本有什么区别？

普通脚本通常只验证一条固定路径；TaskLens 让单 Agent 根据页面状态选择动作，并把每一步的观察、操作、断言和错误保存下来，支持失败复盘、运行比较和后续回归。

### 为什么保留上游 Harness？

上游已经处理真实浏览器连接、CDP 会话、标签页管理和基础 helper。二开重点放在 Agent Runtime、结果校验和测试证据层，既减少重复代码，也让上游同步和回退边界清晰。

### 为什么 SQLite 而不是直接上云数据库？

首版目标是单机可演示、可复现和低运维成本。SQLite 足以支撑任务、步骤和统计查询，同时保留仓储接口，后续可以迁移到 PostgreSQL；云端多租户和权限不放进九天首版。

## 5. 开发兑现清单

简历中的完成态表述必须由以下证据支撑：

| 简历能力 | 必须落地的证据 |
| --- | --- |
| 单 Agent Runtime | `agent_runtime.py`、状态机单测、成功/超时/重试样例 |
| Browser Adapter | CDP/daemon/helper 适配代码、连接失败测试 |
| 任务和步骤记录 | `TaskSpec`、`RunRecord`、`StepRecord` 模型及 SQLite schema |
| 结果校验 | 至少三类断言：文本、元素状态、结构化结果 |
| 故障归因 | environment/connection/action/assertion/timeout/model 分类测试 |
| API 与 Dashboard | FastAPI 路由、响应 schema、页面截图和 Playwright 报告 |
| 工程质量 | Pytest 覆盖率、Ruff、Pyright、Docker 启动和 README 演示 |

## 6. 不能混入本项目的表述

为了保持项目边界清楚，除非后续明确实现，不要写：多 Agent 协作、人工接管、跨机器并发调度、在线多租户、自动根因分析或线上用户数据。它们不是本项目的核心卖点，反而会增加面试时的追问风险。

