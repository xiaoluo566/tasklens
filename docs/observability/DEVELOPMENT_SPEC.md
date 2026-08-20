# TaskLens 项目开发能力规格

> 这是简历项目的开发交接文档。它把产品目标、工程约束、模块接口和验收证据固定下来，供后续编码、测试和面试演示使用。执行架构冻结为单 Agent；当前仅完成规格设计，文中的“计划/拟议”需要在后续开发中兑现。

## CAPABILITY

TaskLens 基于 `browser-harness` 与 CDP 构建本地优先的单 Agent AI 浏览器测试能力：用户提交自然语言目标后，由一个 Agent Runtime 循环完成页面观察、动作选择、helper 执行、结果校验和有界重试；测试开发者可以在不改变原任务退出码的前提下，查看脱敏后的运行摘要、helper 步骤、失败阶段和耗时，并通过页面进行筛选和复盘。开源依赖提供浏览器连接与 CDP 控制，TaskLens 负责单 Agent 执行闭环和测试证据层。

### Existing foundation and extension boundary

| 开源基础 | TaskLens 实现 | 解决的问题 |
| --- | --- | --- |
| `run.py` 的 stdin 执行和 helper trace | `TaskSpec`、Agent Runtime、步骤预算和终态模型 | 任务状态分散、循环和终止条件不可控 |
| daemon 的 CDP WebSocket、Tab/session 和 helper | Browser Adapter、统一动作协议和错误映射 | Agent 与浏览器生命周期耦合、连接故障难定位 |
| `recorder.py` 的事件/截图上下文和页面 DOM 状态 | 断言、证据引用、首次偏离步骤和 `failure_kind` | 动作成功与业务正确混淆、失败无法归因 |
| 本地配置目录和过程输出 | Pydantic 模型、SQLite repository、FastAPI 查询与脱敏策略 | 日志分散、历史不可检索、敏感信息和观测故障风险 |

## CONSTRAINTS

### 固定规则

1. **依赖兼容**：保留 `browser-harness` Python 包名和既有 CLI/helper 契约；TaskLens 能力通过独立模块、Browser Adapter 和旁路 Hook 接入。
2. **主流程优先**：观测模块出现写入、序列化或页面服务异常时，原脚本输出和退出码保持不变。
3. **本地优先**：默认存储在现有配置目录，默认只监听 `127.0.0.1`，不上传任务原文，不提供远程写接口。
4. **数据最小化**：不保存完整 stdin 脚本、Cookie、页面 HTML 或默认截图内容；URL 去除 query/fragment，敏感键先脱敏再截断。
5. **有界资源**：任务摘要、输出尾部、单步参数、步骤数量、历史条数和 API `limit` 都必须有硬上限。
6. **证据可回溯**：页面上的统计必须能回溯到一个 `run_id` 和对应的 `StepRecord`，不根据缺失数据猜测结论。
7. **可撤回**：Dashboard、API 和观测 Hook 可以独立关闭或回退，不触碰 daemon 和浏览器控制核心。
8. **单 Agent**：P0/P1 只维护一个有状态 Agent Runtime；计划、执行、观察和校验在同一运行上下文内完成，不引入多 Agent 通信、调度和结果合并。

### 设计偏好

- 首版使用 SQLite 和 FastAPI 实现单机 MVP；通过 repository 接口隔离存储实现，后续可以迁移 PostgreSQL。
- Playwright 用于 Dashboard 的真实浏览器 E2E，不把它误写成上游控制适配器。
- 先冻结数据契约和失败语义，再实现页面，避免出现“页面先做出来但无法解释数据”的情况。

## IMPLEMENTATION CONTRACT

### Actors

| 角色 | 目标 | 入口 |
| --- | --- | --- |
| 测试开发者 | 定位失败、查看耗时和历史 | CLI、Dashboard |
| Agent/Harness 开发者 | 检查 helper 行为和浏览器后端差异 | 运行详情、API |
| 单 Agent Runtime | 根据任务目标选择动作并判断结果 | `agent_runtime.py` |
| Harness Runner | 执行原始 stdin 脚本并决定真实退出码 | `run.py` |

### Surfaces and ownership

| 组件 | 计划职责 | 明确不负责 |
| --- | --- | --- |
| `src/browser_harness/run.py` | 捕获开始/结束、已有 helper trace 和退出状态 | 存储实现、HTML 生成 |
| `src/browser_harness/agent_runtime.py` | 单 Agent 状态机、观察/动作/校验循环、步骤预算和有界重试 | 多 Agent 编排、浏览器底层连接 |
| `src/browser_harness/browser_adapter.py` | 封装会话、页面观察和 helper 动作，统一错误映射 | 模型决策、证据存储 |
| `src/browser_harness/observability.py` | 脱敏、Pydantic 模型、证据构建 | 浏览器连接、监听端口 |
| `src/browser_harness/storage.py` | SQLite 追加、读取、过滤、聚合和迁移边界 | 浏览器连接、页面展示 |
| `src/browser_harness/dashboard.py` | FastAPI 路由、自包含页面、启动参数 | 浏览器动作实现、改写历史记录 |
| `tests/unit/` | 脱敏、生命周期、存储和边界测试 | 真实浏览器业务断言 |
| `tests/integration/` / E2E | API 和用户关键闭环 | 修改产品数据 |

### Single-agent lifecycle

```text
received -> planning -> observing -> acting -> verifying -> succeeded
                              ^             |              |
                              |             +-> retrying --+
                              |                            |
                              +----------------------------+
                                                            \-> failed/timeout/cancelled
```

一个 Runtime 内部可以重新观察页面并重试可恢复动作，但不会创建第二个 Agent。每个任务必须设置最大步数、总超时和重试预算；副作用动作默认不自动重试，除非任务契约显式声明幂等。

`status` 描述任务最终状态；`failure_kind` 描述失败归因，建议枚举为：

```text
none | environment | connection | action | assertion | timeout | cancelled | model | unknown
```

观测自身的错误不覆盖主任务状态，只记录到 Dashboard health 或服务日志中。

### Data contract

```json
{
  "run_id": "uuid",
  "task_name": "redacted-short-name",
  "started_at": "ISO-8601 UTC",
  "finished_at": "ISO-8601 UTC",
  "duration_seconds": 3.12,
  "status": "success | failed | timeout | cancelled",
  "failure_kind": "none | environment | connection | action | assertion | timeout | cancelled | model | unknown",
  "first_deviation_step": 3,
  "browser_backend": "local | cdp | cloud | unknown",
  "exit_code": 0,
  "output_tail": "redacted-bounded-output",
  "goal": "redacted-task-goal",
  "max_steps": 30,
  "steps": [
    {
      "sequence": 1,
      "helper": "goto",
      "observation_summary": "redacted-bounded-page-state",
      "action": "click",
      "args_summary": "redacted-bounded-args",
      "duration_seconds": 0.8,
      "status": "success | failed",
      "failure_kind": "none | environment | connection | action | assertion | timeout | model | unknown",
      "assertions": [{"name": "element-visible", "status": "passed", "evidence": "bounded-ref"}],
      "error_summary": null
    }
  ]
}
```

P0 必须稳定的字段：`run_id`、时间、状态、退出码、浏览器后端、步骤名称、步骤耗时、`failure_kind`、`first_deviation_step` 和脱敏错误摘要。首次偏离步骤取最早失败的动作或断言；无法定位时必须返回 `null`，不能猜测。

### Task contract

单 Agent 的输入采用结构化任务契约，避免把自然语言直接当作无限制脚本执行：

```json
{
  "task_id": "checkout-smoke",
  "goal": "完成结算并确认订单状态",
  "constraints": {"max_steps": 30, "timeout_seconds": 120},
  "assertions": [
    {"type": "text", "selector": "[data-testid=order-status]", "expected": "Paid"}
  ],
  "retry_policy": {"max_retries": 1, "idempotent_actions_only": true}
}
```

Runtime 输出 `AgentResult`，至少包含 `status`、`failure_kind`、`answer_summary`、`run_id` 和断言结果；原始页面内容和敏感输入不得直接写入结果。

### API contract

所有响应使用统一 envelope：

```json
{"success": true, "data": {}, "error": null, "meta": {}}
```

| 方法 | 路径 | 行为 | 验收证据 |
| --- | --- | --- | --- |
| POST | `/api/tasks` | 校验 `TaskSpec` 并启动一次单 Agent 任务 | 合法请求返回 `run_id`，非法请求 400 |
| GET | `/api/health` | 返回服务和存储状态 | 200、无堆栈泄漏 |
| GET | `/api/summary` | 返回总量、成功/失败和平均耗时 | 与样例 SQLite 数据一致 |
| GET | `/api/runs` | `limit/status/browser` 过滤 | 合法参数 200，非法参数 400 |
| GET | `/api/runs/{run_id}` | 返回一次运行和步骤 | 存在 200，不存在 404 |
| GET | `/` | 返回 Dashboard 页面 | 空状态和错误状态可读 |

除本地受校验的 `/api/tasks` 外，不提供修改历史的 POST、PUT、DELETE；Dashboard 本身保持只读，任务执行由单 Agent Runtime 负责。

### Failure and recovery contract

| 场景 | 原始任务 | 观测层 |
| --- | --- | --- |
| 记录目录不可写 | 保持原退出码 | 吸收异常，health 标记不可用 |
| SQLite 查询/事务失败 | 不受影响 | health 标记存储异常并保留内存摘要 |
| API 参数非法 | 不受影响 | 返回结构化 400 |
| 运行 ID 不存在 | 不受影响 | 返回结构化 404 |
| Dashboard 端口占用 | 不受影响 | 启动命令失败并给出可操作错误 |

## NON-GOALS

本轮不负责：

- 多 Agent 协作、Agent 间消息总线和动态角色调度；
- 重新实现 CDP、浏览器发现、标签页管理或页面操作 helper；
- 云端多租户、账号权限、远程任务调度和在线数据同步；
- LLM 自动根因分析和自动修复；
- 完整测试用例管理、复杂回归调度和生产告警；
- 任何未经过真实测试和数据统计支撑的成功率、性能提升或市场规模结论。

## RESUME EVIDENCE MAP

简历中的每条能力必须对应代码、测试或文档证据：

| 编号 | 简历可用表述 | 当前证据 | P0 完成后的证据 |
| --- | --- | --- | --- |
| R1 | 设计单 Agent Runtime 状态机和有界重试 | 本规格、`PROJECT_ANALYSIS.md` | `agent_runtime.py`、状态机测试、执行日志 |
| R2 | 抽象 Browser Adapter 并隔离浏览器生命周期 | `PROJECT_ANALYSIS.md`、`ARCHITECTURE.md` | Adapter 实现、`run.py` Hook diff、回归测试 |
| R3 | 设计 `RunRecord`/`StepRecord`、断言和失败语义 | `ARCHITECTURE.md`、本规格 | 模型单测、样例 SQLite、API 响应 |
| R4 | 设计脱敏、限长、回环监听和 fail-open | `PRD.md`、`REQUIREMENTS.md` | 脱敏测试、写入失败测试、安全审查 |
| R5 | 规划只读 API 和 Dashboard | `PRD.md`、`REQUIREMENTS.md` | API 集成测试、页面截图、Playwright 报告 |
| R6 | 建立测试与交付路线 | `ROADMAP.md`、`REQUIREMENTS.md` | 覆盖率报告、真实浏览器冒烟、演示脚本 |

## DELIVERY SLICES

1. **契约冻结**：先写 `TaskSpec`、断言、脱敏规则和 Runtime 状态机测试；不动浏览器控制逻辑。
2. **单 Agent 闭环**：实现观察—动作—校验循环、步骤预算、总超时和幂等重试。
3. **存储闭环**：实现 SQLite schema、仓储接口、运行/步骤追加、过滤、汇总和迁移边界。
4. **执行接入**：在 `run.py` 的正常返回、`SystemExit` 和异常路径生成记录，并验证 fail-open。
5. **API 闭环**：实现 health、summary、runs、detail、任务启动和统一错误 envelope。
6. **页面闭环**：实现空状态、运行列表、失败详情、耗时排序、步骤回放和窄屏布局。
7. **验证交付**：运行开源基线回归、覆盖率、Ruff、Pyright、Docker 启动和真实浏览器 E2E，保存可公开的脱敏证据。

## OPEN QUESTIONS

- P0 的模型提供商采用哪一种可替换适配器，如何在无密钥环境运行确定性测试？
- 历史记录是否增加磁盘配额和清理命令？
- 断言 DSL 首版固定文本、元素状态和结构化结果三类，还是开放插件接口？
- 是否在 P1 引入 `source_digest` 用于同一任务的版本 Diff？
- 云服务器演示是否只通过 SSH 隧道访问，避免开放未认证端口？

## HANDOFF

当前能力已达到“可直接进入实现”的条件，前提是先确认模型适配器、断言 DSL 和存储上限。下一步按 `ROADMAP.md` 执行 TDD：先补单 Agent Runtime 测试，再实现 `agent_runtime.py`、证据存储、`run.py` 接入、API 和页面。完成代码与验证后，直接使用 `RESUME_DRAFT.md` 的项目成稿。
