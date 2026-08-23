# TaskLens 四个技术要点详解

> 本文用于理解和实现简历中的四个技术要点。它解释每个要点的业务含义、运行机制、模块职责、解决的问题和面试时的边界。
>
> **状态说明：** P0 核心代码已落地并有自动化契约测试；真实 Chrome/CDP、真实模型、Docker 和窄屏 Playwright 仍需在目标环境补证。文中把“已实现代码”和“待环境验收”分开标注，不能把设计目标当成实测结果。

## 1. 项目整体在解决什么问题

`browser-harness` 已经解决了“如何连接真实浏览器并执行点击、输入、导航等操作”。但只完成一次浏览器动作，并不能回答测试开发中的几个关键问题：

- 任务是否真的达成了业务目标？
- 失败发生在浏览器环境、连接、动作还是业务断言？
- 哪一步最早出现了可观察的偏离？
- 这次运行能否被历史查询、复盘和回归测试使用？

TaskLens 的增量不是重写 CDP 或浏览器 helper，而是补齐单 Agent 的执行控制、业务结果验证和测试证据层：

```text
自然语言目标
    ↓
TaskSpec（任务契约）
    ↓
单 Agent Runtime：观察 → 选择动作 → 执行 → 再观察
    ↓
Browser Adapter（CDP/helper 适配层）
    ↓
Assertion（业务断言）
    ↓
AgentResult + RunRecord/StepRecord
    ↓
SQLite 持久化 → FastAPI 查询 → Dashboard 复盘
```

项目固定为**单 Agent**：同一个有状态 Runtime 负责观察、决策、执行和校验，不引入多 Agent 通信、角色调度或结果合并。

## 2. 用一个例子理解完整链路

假设用户输入：

> 在测试电商网站搜索“机械键盘”，加入购物车，提交测试订单，并确认订单状态为 Submitted。

TaskLens 不会把整句话直接当成一段无限制 Python 执行，而是形成结构化任务契约：

```json
{
  "task_id": "checkout-smoke",
  "goal": "搜索机械键盘并提交测试订单",
  "constraints": {
    "max_steps": 20,
    "timeout_seconds": 90
  },
  "assertions": [
    {
      "type": "text",
      "selector": "[data-testid=order-status]",
      "expected": "Submitted"
    }
  ],
  "retry_policy": {
    "max_retries": 1,
    "idempotent_actions_only": true
  }
}
```

一次运行大致经过：

1. Runtime 创建 `run_id`，加载任务预算和断言。
2. Agent 观察当前页面，得到 URL、标题和可交互元素摘要。
3. Agent 产生结构化动作，例如 `Click`、`Type` 或 `Wait`。
4. Browser Adapter 将动作转换为 browser-harness helper 调用。
5. Adapter 返回动作结果和耗时，Runtime 再次观察页面。
6. Assertion 检查页面是否达到业务终态。
7. Runtime 根据结果继续、有限重试或结束。
8. 观测层将任务、步骤、断言和失败证据脱敏后写入 SQLite。
9. Dashboard 通过 FastAPI 查询运行列表和详情。

例如点击提交按钮没有抛异常，但页面状态仍为 `Pending`，最终结果应是：

```text
动作：成功
页面：有响应
业务断言：失败
任务：失败
failure_kind：assertion
```

这正是 TaskLens 与“能调用浏览器的脚本”之间的区别。

## 3. 要点一：单 Agent 执行状态链

简历表述：

> 围绕真实浏览器任务的单 Agent 执行链路，搭建从 `TaskSpec`、`Observation`、`Action` 到 `Assertion`、`AgentResult` 的状态链，引入最大步数、任务级超时和幂等重试约束，将自然语言目标收敛为可控执行流程，解决 Agent 状态分散、异常循环和终止条件不清的问题。

### 3.1 这条状态链分别是什么

| 对象 | 含义 | 主要内容 | 谁负责产生 |
| --- | --- | --- | --- |
| `TaskSpec` | 一次任务的输入契约 | 目标、约束、断言、重试策略 | API/CLI + Pydantic 校验 |
| `Observation` | Agent 在某一步看到的页面状态 | URL、标题、页面摘要、可交互元素、上一步结果 | Browser Adapter |
| `Action` | Agent 请求执行的一个结构化动作 | 动作类型、目标、参数、幂等性 | Agent Runtime/模型适配器 |
| `Assertion` | 对业务结果的独立判定 | 预期值、实际值、证据、通过/失败 | Assertion Evaluator |
| `AgentResult` | 任务最终结果 | 状态、失败类型、摘要、断言结果、`run_id` | Agent Runtime |

这些对象把原本混在模型上下文、终端输出和浏览器状态中的信息拆成了可验证的边界。

### 3.2 Runtime 如何运行

Runtime 可以建模为一个有限状态机：

```text
received
   ↓
planning → observing → acting → verifying
                 ↑         ↓          ↓
                 └──── retrying ←─────┘
                              ↓
                   succeeded / failed / timeout / cancelled
```

- `received`：校验 `TaskSpec`，创建运行上下文。
- `planning`：让单 Agent 根据目标和当前证据选择下一步，不创建第二个 Agent。
- `observing`：获取页面状态并压缩成 `Observation`。
- `acting`：执行一个结构化 `Action`。
- `verifying`：执行断言或判断是否需要继续观察。
- `retrying`：仅对符合策略的动作进行有限重试。
- 终态：成功、业务失败、超时或取消。

每次循环都必须检查：

```text
已用步数 < max_steps
且 当前时间 - started_at < timeout_seconds
且 retry_count < max_retries
```

### 3.3 为什么必须使用结构化 Action

让模型直接生成任意 Python 会带来三个问题：参数格式不可控、难以记录和回放、可能执行任务范围之外的操作。TaskLens 应将输出限制为动作协议，例如：

```text
Goto(url)
Click(target)
Type(target, value)
Scroll(direction)
Wait(condition)
Finish(reason)
```

每个动作先做 schema 校验，再交给 Adapter 执行。动作失败时，Runtime 记录失败类型，而不是把未经处理的异常直接返回给用户。

### 3.4 幂等重试的边界

幂等不是“所有失败都再试一次”。重复执行不会产生额外业务副作用的动作，才可能自动重试：

| 动作 | 默认是否重试 | 原因 |
| --- | --- | --- |
| 重新获取页面状态 | 是 | 只读操作 |
| 等待元素出现 | 是 | 不改变业务状态 |
| 导航到同一 URL | 通常是 | 可在任务契约中声明 |
| 点击提交/支付 | 否 | 可能重复产生副作用 |
| 删除、发送消息 | 否 | 不应自动重复 |

重试也要消耗步骤和时间预算，不能通过重试绕过任务上限。

### 3.5 这一点解决的问题

- 用统一 `TaskSpec` 解决不同任务输入格式不一致；
- 用状态机解决 Agent 状态散落、循环和终止条件模糊；
- 用步数/超时预算控制资源消耗；
- 用幂等策略降低重复副作用；
- 用 `AgentResult` 让 API、存储和测试拥有稳定的最终结果契约。

### 3.6 实现边界

**TaskLens 负责：** `TaskSpec`、Runtime 状态机、动作协议、预算控制、重试策略和 `AgentResult`。

**上游负责：** 浏览器连接、Tab 管理和底层 helper 执行。

## 4. 要点二：Browser Adapter 与错误映射

简历表述：

> 封装 CDP WebSocket、Tab/session 管理与浏览器 helper 能力，设计统一的 Browser Adapter、动作协议和错误映射层，将 Agent 决策与浏览器生命周期解耦，解决环境差异导致的接口不一致、连接故障难定位和底层依赖难替换的问题。

### 4.1 为什么不能让 Runtime 直接调用 CDP

真实浏览器控制涉及：

- CDP WebSocket 连接；
- daemon 生命周期；
- 浏览器和 Tab 发现；
- session 有效性；
- 页面跳转和元素定位；
- helper 的参数和异常格式。

如果这些细节全部写在 Agent Runtime 中，Runtime 就无法脱离真实浏览器进行测试，也无法在上游 helper 变化时保持稳定。

### 4.2 Adapter 的职责

TaskLens 的 Adapter 可以提供类似下面的接口：

```python
class BrowserAdapter(Protocol):
    async def attach(self) -> None: ...
    async def observe(self) -> Observation: ...
    async def execute(self, action: Action) -> ActionOutcome: ...
    async def capture_evidence(self) -> EvidenceRef: ...
    async def close(self) -> None: ...
```

Runtime 只关心 `Action` 和 `ActionOutcome`。例如：

```text
Runtime：execute(Click(target="Submit Order"))
Adapter：检查 session → 调用 helper → 记录耗时 → 返回统一结果
```

Adapter 不负责决定任务目标是否达成，也不负责写数据库。

### 4.3 错误映射

底层异常需要被转换成稳定的 `failure_kind`：

| 底层情况 | 统一分类 | 含义 |
| --- | --- | --- |
| 找不到浏览器、启动环境不完整 | `environment` | 执行环境没有准备好 |
| WebSocket 断开、Tab/session 失效 | `connection` | 浏览器连接链路异常 |
| 元素不存在、点击被遮挡、参数非法 | `action` | 动作本身没有完成 |
| 等待或任务超过预算 | `timeout` | 时间约束触发 |
| 动作完成但业务条件不满足 | `assertion` | 业务结果错误 |
| 模型输出无法解析或不符合动作协议 | `model` | 决策输出不可执行 |
| 无法根据现有证据分类 | `unknown` | 不猜测根因 |

统一分类可以让 Dashboard 按失败类型统计，也可以让重试策略只对特定类型生效。

### 4.4 Adapter 带来的可测试性

Runtime 单元测试不应每次都启动 Chrome。可以使用 Fake Adapter 返回预设页面：

```text
Observation 1：登录页
Action：输入账号
Outcome：成功
Observation 2：商品页
Action：点击提交
Outcome：成功
Assertion：订单状态为 Submitted
```

只有 Adapter 集成测试和少量 Playwright 冒烟测试才连接真实浏览器。这样可以把“状态机正确性”和“浏览器兼容性”分开验证。

### 4.5 这一点解决的问题

- 解耦 Agent 决策和浏览器生命周期；
- 统一本地 CDP、显式 CDP 等后端的接口；
- 将底层异常转换为可统计的失败语义；
- 允许 Fake Adapter 做快速、确定性的 Runtime 测试；
- 降低未来替换 helper 或浏览器后端的改造范围。

### 4.6 开源基础与个人增量

`browser-harness` 已提供 CDP WebSocket、daemon、Tab/session 和常用 helper。TaskLens 的增量是围绕这些能力建立 Adapter、Action/Outcome 契约和错误映射，不声称重新实现 CDP。

## 5. 要点三：业务断言、执行证据与失败归因

简历表述：

> 引入文本、元素状态和结构化结果三类断言，结合 helper trace、DOM/状态快照、首次偏离步骤与 `failure_kind`，建立动作结果、页面证据和业务终态的分层判定链，避免将 helper 成功误判为业务成功，并支持模型、环境和动作失败归因。

### 5.1 三层判定链

一次操作必须分成三层：

```text
动作层：helper 是否成功返回？
       ↓
页面层：页面状态发生了什么变化？
       ↓
业务层：是否满足任务定义的终态断言？
```

例如点击提交按钮没有抛异常，只能得到“动作层成功”。如果订单状态仍为 `Pending`，业务层断言仍然失败。

### 5.2 三类断言

#### 文本断言

验证用户可见文本：

```text
订单状态包含 “Submitted”
页面标题等于 “Order Complete”
错误提示不包含 “Payment failed”
```

适合验证结果文案、提示信息和标题。

#### 元素状态断言

验证元素是否存在、可见或处于指定交互状态：

```text
成功提示可见
提交按钮已禁用
复选框已勾选
登录按钮已经消失
```

适合验证页面交互状态，而不仅是文本内容。

#### 结构化结果断言

先从页面或 helper 结果中提取结构化对象，再比较字段：

```json
{
  "order_id": "T20260821",
  "status": "SUBMITTED",
  "total": 399
}
```

可以验证 `status == "SUBMITTED"`、`total == 399` 或 `order_id != null`。结构化结果更适合回归测试和精确失败信息。

### 5.3 证据如何关联

每个 `StepRecord` 至少需要关联：

- 动作类型和脱敏参数；
- 动作开始/结束时间及耗时；
- 动作前后的页面摘要；
- helper 是否抛异常；
- 断言名称、预期值、实际值和结果；
- 证据引用（例如 DOM 摘要或截图 hash）；
- 统一 `failure_kind`。

因此失败详情可以表达为：

```text
第 6 步：Click("Submit Order")，动作成功
第 7 步：页面状态为 Pending
断言：expected=Submitted，actual=Pending
最终状态：failed
failure_kind：assertion
```

### 5.4 首次偏离步骤的准确含义

`first_deviation_step` 是“最早能从已保存证据中观察到偏离”的步骤，不等于模型真正犯错的内部原因。

建议规则：

1. 先找最早失败的 Action；
2. 如果动作都成功，再找最早失败的 Assertion；
3. 如果证据不足，返回 `null`，不能猜测。

例如第 3 步选错了商品，但第 7 步检查金额时才发现：第 7 步是首次可观察偏离；不能在没有第 3 步证据的情况下宣称已经定位到根因。

### 5.5 判定权边界

最终判定应遵循：

```text
确定性业务断言 > 页面状态证据 > Agent 自述
```

Agent 说“任务完成”不能替代断言。若任务没有任何终态断言，最多只能说明 Agent 停止了执行，不能严格证明业务成功；首版应要求关键测试任务至少声明一个终态断言。

### 5.6 这一点解决的问题

- 防止 helper 成功被误判为业务成功；
- 将页面现象和任务结论分开；
- 为失败提供可复核的页面证据；
- 区分模型决策、环境连接、动作执行和业务断言失败；
- 支持定位首次可观察偏离，而不是凭感觉猜根因。

## 6. 要点四：运行证据链、查询观测与质量保障

简历表述：

> 通过执行追踪、录制事件与本地优先约束，建立 Pydantic v2 + SQLite 的任务—步骤—断言证据链，配套 FastAPI 查询、敏感字段脱敏、长度限制和 fail-open 降级，解决日志分散、历史不可检索和观测故障影响主任务的问题；使用 Pytest、Pyright 与 Playwright 契约测试验证状态流转和接口页面边界，真实浏览器流程留待目标环境补证。

### 6.1 Pydantic v2 的职责

Pydantic 是系统边界的契约层，负责：

- 校验 `TaskSpec`、Action 和 API 请求；
- 限制最大步数、任务超时和重试次数；
- 限制目标、页面摘要、错误和参数长度；
- 让 Runtime、存储和 API 共享稳定字段；
- 防止未经检查的模型输出直接进入浏览器或数据库。

它不是简单的序列化工具，而是把不可信输入转成明确的领域对象。

### 6.2 SQLite 证据模型

逻辑关系为：

```text
RunRecord
 ├── StepRecord 1
 │    ├── AssertionRecord A
 │    └── AssertionRecord B
 ├── StepRecord 2
 │    └── AssertionRecord C
 └── StepRecord 3
```

每次运行用 `run_id` 关联任务、步骤和断言，至少保存：

- 开始/结束时间和总耗时；
- 最终状态、退出码和 `failure_kind`；
- 浏览器后端和首次偏离步骤；
- 每一步动作、页面摘要和耗时；
- 每个断言的预期值、实际值和证据引用；
- 脱敏后的错误摘要。

Dashboard 上的统计必须能回溯到 `run_id` 和具体 `StepRecord`，不能用缺失数据推断成功率或根因。

### 6.3 FastAPI 与 Dashboard 的职责

首版 API 可以保持只读查询为主：

| 接口 | 作用 |
| --- | --- |
| `POST /api/tasks` | 校验 TaskSpec 并启动一次单 Agent 任务 |
| `GET /api/health` | 查看服务和 SQLite 状态 |
| `GET /api/summary` | 查看总量、成功/失败和平均耗时 |
| `GET /api/runs` | 按状态、浏览器后端和数量筛选运行 |
| `GET /api/runs/{run_id}` | 查看某次运行的步骤、断言和错误证据 |
| `GET /` | 展示 Dashboard 页面 |

页面至少应支持：最近运行列表、失败类型、耗时排序、失败详情、断言结果和步骤时间线。

这里的“步骤回放”首版指按时间线查看已保存的动作和证据，不承诺重新执行浏览器任务。确定性重放属于后续 Replay/Regression 能力。

### 6.4 脱敏、限长和本地优先

浏览器任务可能包含账号、Cookie、订单信息和带 token 的 URL，因此必须：

- 先去除 URL 的 query 和 fragment，再写入存储；
- 将 `token`、`password`、`cookie`、`authorization`、`api_key`、`secret` 等字段替换为 `[REDACTED]`；
- 输入值默认不保存明文；
- 页面摘要、参数和异常先脱敏再截断；
- 默认不保存完整 HTML、Cookie 和原始脚本；
- 默认监听 `127.0.0.1`，不提供未经保护的远程写接口；
- 对单步数量、历史数量和 API `limit` 设置硬上限。

### 6.5 fail-open 的真正含义

fail-open 只适用于观测和存储层，不能改变业务判定：

```text
任务成功 + SQLite 写入失败 → 主任务仍成功，health 标记存储异常
任务断言失败 + SQLite 写入失败 → 主任务仍失败，不能被观测故障“放行”
```

这样可以避免 Dashboard、日志目录或数据库短暂故障反过来改变原 Harness 的退出码。

### 6.6 三类测试工具分别验证什么

#### Pytest

验证 Runtime 状态流转、预算、重试、断言、脱敏、数据库事务失败、API 400/404 和错误映射。Runtime 测试优先使用 Fake Model Provider 和 Fake Browser Adapter，保证确定性。

#### Pyright

检查 `Observation | None`、Action 返回值、Adapter 实现和 API/存储模型之间的类型契约，提前发现边界漂移。

#### Playwright

验证真实 Dashboard：列表加载、失败筛选、详情展开、步骤展示、空状态、错误状态和窄屏布局；同时保留少量真实浏览器任务冒烟，验证 Adapter 与页面的连接。

### 6.7 这一点解决的问题

- 将终端日志变成可查询的运行历史；
- 用 `run_id` 把任务、步骤和断言串起来；
- 防止敏感页面信息直接落盘；
- 让观测故障不破坏主任务结果；
- 让 AI 流程拥有可重复的单元、集成和 E2E 验证证据。

## 7. 四个要点与计划中的代码职责

以下是架构层的代码落点，便于维护和面试说明。它不是要求把所有逻辑放进一个入口文件：

| 能力 | 计划模块 | 主要职责 |
| --- | --- | --- |
| 任务契约与运行状态 | `agent_runtime.py`、领域模型模块 | `TaskSpec`、Observation、Action、AgentResult、状态机、预算 |
| 浏览器控制隔离 | `browser_adapter.py` | CDP/session/helper 适配、ActionOutcome、错误映射 |
| 断言与证据构建 | `observability.py`、assertion 模块 | 三类断言、脱敏、StepRecord、首次偏离和失败语义 |
| 持久化 | `storage.py` | SQLite schema、repository、事务和查询 |
| API 与页面 | `dashboard.py`、前端资源模块 | FastAPI envelope、列表/详情/健康接口和 Dashboard |
| 入口兼容 | 上游既有 CLI 入口 | 接入任务启动和旁路观测，不承载全部业务逻辑 |

`run.py` 只属于上游 CLI 的入口兼容层，不能作为 TaskLens 的架构说明，也不应出现在简历技术要点中。

## 8. 已有能力与 TaskLens 增量

| 能力 | `browser-harness` 当前基础 | TaskLens P0 增量 |
| --- | --- | --- |
| 浏览器连接 | CDP WebSocket、daemon、Tab/session | 统一 Adapter 生命周期 |
| 页面操作 | 导航、点击、输入、等待、截图等 helper | 结构化 Action 和标准化 Outcome |
| 过程记录 | helper trace、recorder 事件/截图 | `run_id`、StepRecord、断言证据和查询模型 |
| 任务执行 | stdin 脚本入口 | 单 Agent Runtime、TaskSpec、预算和重试 |
| 业务正确性 | 没有统一终态断言 | 文本、元素状态、结构化结果断言 |
| 观测界面 | 没有 Dashboard | FastAPI API、运行列表、失败详情和步骤时间线 |
| 测试闭环 | 上游模块测试 | TaskLens Runtime/API/UI/真实浏览器测试 |

面试时应明确说：上游提供浏览器执行原语，个人项目负责把这些原语组织成可验证、可查询、可复盘的单 Agent 测试平台。

## 9. 不应过度承诺的边界

为了保证简历表述和后续代码一致，以下说法需要谨慎：

- `first_deviation_step` 是首次可观察偏离，不等于完整的模型根因分析；
- “步骤回放”首版是证据时间线查看，不等于确定性重新执行；
- fail-open 只保护观测层，不会把断言失败判成成功；
- 没有真实测试统计时，不写成功率提升、性能提升或稳定性提升百分比；
- 不把 Playwright 描述成 Agent 的底层 CDP 控制器；
- 不把上游已有的 CDP/helper 能力描述成从零实现；
- 当前仓库的 P0 代码和自动化契约测试已经落地；真实 Chrome/CDP、真实模型、Docker 和窄屏 Playwright 仍需按演示手册补做环境验收。

## 10. 面试中的一句话总结

> TaskLens 不重新造浏览器控制，而是在 `browser-harness` 的真实浏览器能力之上，用单 Agent Runtime 管住任务执行，用 Browser Adapter 隔离 CDP 生命周期，用业务断言证明结果正确，再把每一步沉淀成可脱敏查询和回放的测试证据。

## 11. 当前验收状态

- [x] `TaskSpec`、Action、Observation、Assertion、AgentResult 有稳定 schema；
- [x] Runtime 覆盖成功、断言失败、动作异常、超时和取消；
- [x] 最大步数、任务级超时和幂等重试有自动化测试；
- [ ] Browser Adapter 有 Fake 实现和真实 CDP/浏览器冒烟；
- [x] 三类断言不会把 helper 成功直接当成业务成功；
- [x] `failure_kind` 和 `first_deviation_step` 有明确测试；
- [x] SQLite 能按 `run_id` 查询任务、步骤和断言；
- [x] 敏感字段先脱敏、后截断，写入失败不改变主任务退出码；
- [x] FastAPI 有 health、summary、runs、detail 和结构化错误响应；
- [x] Dashboard 能展示列表、失败详情、断言和步骤时间线；
- [ ] Pytest、Pyright、Playwright 和上游回归测试全部通过（TaskLens 契约测试和静态检查已通过；真实浏览器 E2E 待补，上游 Windows checkout 有 1 个既有资源链接失败）；
- [x] README、简历表述和实现证据保持一致。
