# TaskLens 项目简介（简历与面试用）

## 一句话定位

TaskLens 是一个单 Agent AI 浏览器测试与运行观测平台：基于 `browser-harness` 与 CDP 构建浏览器操作层，让 Agent 在真实浏览器中执行复杂网页任务，并留下可验证、可脱敏、可回放的测试证据。

## 项目介绍

平台以自然语言目标和结构化 `TaskSpec` 为入口，由单 Agent Runtime 负责页面观察、动作选择、Browser Adapter 调用、业务断言和有界重试；运行过程通过 Pydantic v2 建模并保存到 SQLite，FastAPI 提供任务启动、统计、筛选和详情接口，Dashboard 展示步骤状态、失败归因、断言结果、耗时和回放信息。

## 技术栈

Python 3.11+、FastAPI、Pydantic v2、CDP、WebSocket、SQLite、Pytest、Playwright、Ruff、Pyright、Docker。

## 核心模块

| 模块 | 作用 |
| --- | --- |
| `agent_runtime.py` | 单 Agent 状态机：观察、动作、校验、重试和终态判定 |
| `model_provider.py` | 确定性 Demo 与 OpenAI-compatible Provider，限制模型输出为结构化动作 |
| `tasklens_runtime.py` | 组合 Browser Adapter、Provider 与 Runtime，提供 CLI/Dashboard 默认入口 |
| `browser_adapter.py` | 复用上游 daemon/CDP/helper，提供统一浏览器动作接口 |
| `observability.py` | 脱敏、长度限制、RunRecord/StepRecord/AssertionRecord 构建 |
| `storage.py` | SQLite schema、repository、统计和查询 |
| `dashboard.py` | FastAPI 路由、统一错误 envelope、页面和步骤回放 |

## 项目亮点

1. **单 Agent 闭环**：在一个运行上下文中完成观察—行动—校验，设置最大步数、总超时和幂等重试预算，控制 Agent 循环风险。
2. **结果而非只看动作**：helper 调用成功不直接等价于业务成功，文本、元素状态和结构化结果断言独立记录。
3. **证据可追溯**：每次运行拥有 `run_id`，每个步骤记录观察摘要、动作、耗时、断言和失败归因，可从总览下钻到首次偏离步骤。
4. **安全与降级**：URL 去除 query/fragment，Token/Cookie/Authorization 等字段脱敏后再截断；存储或观测故障不改变主任务退出码。
5. **依赖隔离**：不重复实现 CDP 控制和浏览器生命周期，通过 Browser Adapter 隔离开源依赖变化，保留升级和回退空间。

## 简历描述

> **TaskLens｜单 Agent AI 浏览器测试与运行观测平台（Python / FastAPI / CDP / SQLite）**
> 基于 `browser-harness` 与 CDP 构建浏览器操作层，形成“任务理解 → 页面观察 → 动作执行 → 结果校验 → 失败归因 → 有界重试 → 终态判定”的单 Agent 闭环；使用 Pydantic v2 + SQLite 沉淀脱敏运行证据，并通过 FastAPI + Dashboard 展示失败步骤、断言结果、helper 耗时和任务回放，采用 fail-open 策略隔离观测故障。模型接入通过可替换 Provider 隔离，提供零网络 Demo 模式和 OpenAI-compatible 模式。

## 面试叙事主线

### 为什么不重新实现浏览器自动化？

`browser-harness` 已经处理真实浏览器连接、CDP 会话、标签页管理和基础 helper。TaskLens 把个人开发重点放在单 Agent Runtime、业务结果校验和测试证据层，减少重复代码并明确依赖边界。

### 为什么只使用单 Agent？

单 Agent 让观察、动作、校验、重试和证据写入共享同一状态上下文，更适合九天内把执行闭环做深；多 Agent 通信和调度不是本项目的必要复杂度。

### 为什么 SQLite？

项目首版面向单机演示和可复现测试，SQLite 足以支撑运行、步骤、断言和统计查询；通过 repository 接口保留后续迁移到 PostgreSQL 的空间。

### 如何保证 Agent 真的完成业务？

把“动作成功”和“业务结果正确”分开：动作只记录 helper 执行结果，断言模块再检查文本、元素状态或结构化结果，最终状态由断言和任务契约共同决定。

## 简历量化字段

以下数字只能填入真实测量值：测试数量、覆盖率、任务步数上限、平均响应时间、Docker 启动时间、真实浏览器成功/失败样例数量。
