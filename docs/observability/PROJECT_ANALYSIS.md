# TaskLens 项目全景分析：上游能力、短板与二开路线

> 本文档回答三个问题：上游项目到底是什么、它目前缺什么、TaskLens 应该怎样做出有深度且适合测开岗位的二次开发。当前仓库仍处于开发前设计阶段。

## 1. 先给结论

`browser-use/browser-harness` 不是一个传统的 Web 自动化测试平台，而是一个面向 Agent 的“真实浏览器执行层”。它把 Python stdin 脚本、CDP WebSocket、浏览器操作 helper 和可选录制连接起来，让 Agent 能够直接操作用户已经打开的浏览器。

它已经很好地解决了“怎么操作浏览器”，但还没有完整解决“单 Agent 怎么证明任务做对了、失败在哪里、这次是否比上次更好”。

TaskLens 的二开价值不应该是再造一个浏览器控制库，而应该补齐单 Agent 的执行闭环和测试开发视角的证据层：

```text
上游：提供真实浏览器连接和操作原语
TaskLens：让单 Agent 能观察、验证、复盘和比较这些任务
```

这条边界也能和你的第一个 Skill 评估项目形成互补：第一个项目偏向 Skill/Agent 的评估体系与飞轮，TaskLens 偏向真实浏览器执行现场、测试证据和工程化回归。

## 2. 上游项目是什么

### 2.1 用户入口

上游采用 CLI + stdin Python 的交互模型：

```bash
browser-harness <<'PY'
ensure_real_tab()
print(page_info())
PY
```

用户不是先创建测试用例、选择浏览器驱动、再调用大量框架 API，而是直接给 Harness 一段可以执行的 Python。Helper 会被预导入，daemon 会按环境连接本地 Chrome、显式 CDP 地址或 Browser Use Cloud。

### 2.2 一次任务的实际生命周期

```text
1. CLI 读取 stdin 脚本
2. 判断命令类型和是否存在本地/显式/云端浏览器配置
3. ensure_daemon() 启动或复用长生命周期 daemon
4. daemon 通过 IPC/CDP WebSocket 连接浏览器
5. run.py 包装 helpers.py 中的 helper，记录调用名、参数摘要、耗时和异常
6. exec(code, globals()) 执行用户脚本
7. recorder 可选地保存截图与 events.jsonl
8. telemetry 以匿名事件形式发送 CLI 运行信息，并支持 opt-out
```

### 2.3 核心模块分工

| 模块 | 当前职责 | 对 TaskLens 的意义 |
| --- | --- | --- |
| `src/browser_harness/run.py` | CLI 分发、stdin 执行、helper trace、stdout/stderr tail、退出码 | 最适合接入“旁路运行记录”，但不能破坏原生命周期 |
| `src/browser_harness/daemon.py` | 长生命周期 CDP WebSocket、浏览器连接、标签页/会话维护 | 提供真实执行能力，也是稳定性和平台兼容性的主要复杂度来源 |
| `src/browser_harness/helpers.py` | URL、点击、输入、等待、截图、Tab、JS、上传等浏览器原语 | 记录中的 step 应围绕这些 helper 建模，而不是只记录终端文本 |
| `src/browser_harness/recorder.py` | 可选截图和 `events.jsonl`，支持录制/视频流程 | 可作为证据附件入口，但现有格式更偏回放，不是查询型运行模型 |
| `src/browser_harness/telemetry.py` | 匿名 telemetry、环境识别、opt-out | 与个人调试历史不同，TaskLens 不应直接复用其数据边界 |
| `src/browser_harness/admin.py` | doctor、更新、daemon 生命周期、浏览器/配置诊断 | 后续可把诊断结果关联到一次运行，解释“环境失败”与“任务失败” |
| `src/browser_harness/paths.py` | 配置、runtime、tmp、workspace 路径 | TaskLens 记录应复用现有配置目录，不散落到仓库或用户桌面 |

当前仓库有 10 个 Python 测试文件、约 130 个测试函数，主要覆盖既有单元/集成逻辑；上游没有前端应用，因此也没有 Dashboard 或浏览器端到端的观测页面。

## 3. 上游已经做得好的地方

### 3.1 连接真实浏览器，而不是模拟浏览器

它能够复用用户已有浏览器、登录态和真实页面环境，解决很多传统 WebDriver 项目中的“重新登录、环境不一致、页面行为不真实”问题。对 AI 测试而言，这比单纯在 mock DOM 上跑通更接近实际使用场景。

### 3.2 设计足够薄，Agent 上手快

CLI 入口、预导入 helper 和一个长期 daemon 让 Agent 不需要理解大型测试框架。新增能力可以先在 `agent-workspace/` 中形成 helper，再逐步沉淀。

### 3.3 浏览器能力覆盖面完整

`helpers.py` 已经覆盖导航、Tab、DOM/JS、点击、输入、键盘、等待、网络空闲、截图、上传和 HTTP 请求等常用能力，二开不需要从 CDP 原语开始重写。

### 3.4 已经有“过程记录”的雏形

`run.py` 已经对 helper 做了调用包装，`recorder.py` 也可以保存截图和事件。这个基础使得 TaskLens 可以把“已有过程信息”整理成可检索的运行模型，而不是重新插桩所有 helper。

### 3.5 具备本地/显式 CDP/云端的扩展空间

本地 Chrome、`BU_CDP_URL`/`BU_CDP_WS` 和 Browser Use Cloud 让项目具备不同浏览器后端。对二开来说，可以把浏览器后端作为运行维度，比较同一个任务在不同环境的行为。

## 4. 目前真正的不足

下面区分“代码中可以直接观察到的缺口”和“需要通过真实任务继续验证的风险”，避免把推断写成线上事实。

### 4.1 产品层：它不是测试平台

当前入口是“执行一段 Python”，而不是：

- 创建测试用例；
- 定义前置条件、步骤和预期结果；
- 保存基线；
- 做批量回归；
- 对失败进行分类；
- 输出测试报告。

因此它非常适合完成一次任务，却不适合直接回答测试开发最常问的：

> 这次改动让多少任务变差了？失败集中在哪类步骤？能否复现？

### 4.2 证据层：信息分散，缺少稳定运行模型

`run.py` 中的 helper trace 目前是进程内列表，虽然有最大步骤数和参数长度限制，但没有统一的 `run_id`、任务版本、环境快照、结果 schema 或跨运行索引。录制目录又偏向截图和事件回放，telemetry 偏向匿名产品事件，三者之间没有一个面向测试开发的关联键。

结果是：开发者可能知道“刚才失败了”，但很难把一次失败稳定地关联到脚本版本、浏览器后端、helper 步骤和历史趋势。

### 4.3 可观测性层：没有历史查询与趋势视图

上游 CLI 会在终端输出信息，也可以选择录制，但没有本地 Web 页面展示：

- 最近运行列表；
- 成功率和失败率；
- helper 耗时分布；
- 失败步骤排行；
- 不同浏览器后端对比；
- 同一任务的前后版本差异。

这正是最适合做成个人项目展示面的缺口。

### 4.4 验证层：有执行追踪，不等于有正确性判断

helper 调用成功，只能说明操作没有抛异常，不代表业务结果正确。例如点击按钮没有异常，不代表订单状态已变为“已支付”；页面加载成功，不代表关键文案、金额或权限符合预期。

TaskLens 后续需要引入显式断言、结果提取和证据快照，把“操作过程”与“业务正确性”分开。

### 4.5 稳定性层：环境和 daemon 复杂度高

daemon 同时涉及平台浏览器发现、远程调试权限、IPC、异步 WebSocket、标签页状态和进程生命周期。Windows、macOS、Linux、Snap Chromium、本地 Chrome 和云端浏览器的失败原因并不相同。

当前有 doctor 和若干生命周期测试，但还缺一套面向任务的统一失败分类：环境不可用、连接失败、页面等待超时、helper 参数错误、业务断言失败应该分开记录，否则失败率会混在一起。

### 4.6 任务控制层：缺少统一超时、取消和重试语义

单个 helper 有等待参数，但一个完整任务的总超时、取消、重试次数、重试是否幂等、失败后是否清理标签页等规则没有被提升成统一的任务协议。对回归测试来说，任务级控制比单个 helper 的 timeout 更重要。

### 4.7 安全与隐私：已有 opt-out，但还需要更严格的数据治理

telemetry 支持 opt-out 和部分脱敏逻辑，但 CLI 事件结构明确包含 task、output、steps 和 error_message 等字段。真实任务中这些字段可能含 URL、账号状态、页面文本或业务数据，因此二开时不能把 telemetry 当作个人调试数据库直接复用。

TaskLens 应该默认本地存储、先脱敏再截断、默认回环监听，并明确提供“关闭记录”和“清理历史”的边界。若未来允许 `0.0.0.0`，必须增加强提示和访问控制方案。

### 4.8 工程扩展层：缺少稳定的插件/版本契约

Agent 可以在 `agent-workspace/` 中写 helper，但 helper 的版本、输入输出 schema、依赖和兼容范围没有形成独立的插件协议。随着项目增长，TaskLens 需要区分：

- 临时任务 helper；
- 可复用领域 helper；
- 带版本和测试的生产 helper。

### 4.9 测试层：核心逻辑有测试，完整用户闭环还没有

现有测试主要验证 Python 模块、daemon、helper 和 CLI 行为；由于上游没有前端，缺少“执行任务 → 形成证据 → 页面筛选 → 查看详情”的端到端回归。TaskLens 的二开质量不能只用 Python 单元测试证明，还需要 API 集成和真实浏览器冒烟。

## 5. 二次开发方向

### 方向 A：TaskLens Single-Agent Runtime（P0）

**目标**：让一个 Agent 完成观察、动作、结果校验和有界重试，并把每一步变成可查询、可脱敏、可解释的结构化证据。

首版包括：

1. `TaskSpec`：自然语言目标、步数/超时预算、断言和幂等重试策略；
2. `AgentRuntime`：观察页面、选择动作、调用 Browser Adapter、校验断言并生成 `AgentResult`；
3. `RunRecord`/`StepRecord`：`run_id`、时间、任务目标、浏览器后端、退出码、状态、失败归因和步骤耗时；
4. Pydantic v2 + SQLite：校验并持久化运行、步骤和断言，保留 repository 迁移边界；
5. FastAPI + Dashboard：任务启动、总览、失败筛选、单次详情、慢步骤排序和步骤回放；
6. fail-open：记录、存储或观测失败不能改变主任务退出码。

**为什么适合九天**：不改变浏览器控制核心，把有限时间集中在单 Agent 状态机、结果校验、证据模型和测试闭环上，能同时展示 Python、数据建模、API、前端、安全和测试能力。

### 方向 B：TaskLens Assertions（P0/P1）

**目标**：从“helper 成功”升级为“业务结果可验证”。

可以提供轻量任务契约：

```python
task = {
    "name": "checkout-smoke",
    "steps": [
        {"action": "goto", "url": "https://example.test/checkout"},
        {"assert": {"selector": "[data-testid=order-status]", "text": "Paid"}},
    ],
}
```

首版固定文本、元素状态和结构化结果三类断言；不把真实 URL 或敏感字段写入记录，重点是定义断言结果、证据引用和失败类型。

### 方向 C：TaskLens Replay & Diff（P1）

**目标**：支持一次失败任务的复盘与前后运行差异。

- 保存关键步骤的 URL、标题、DOM 摘要、截图 hash，而不是默认保存完整页面；
- 对比两次运行的步骤序列、耗时、状态和断言结果；
- 标记“首次偏离步骤”和“最终失败步骤”；
- 提供可导出的 Markdown/JSON 报告。

这条路线能和你已有的“测试飞轮/自证飞轮”形成接口：TaskLens 负责提供真实浏览器执行证据，上层评估系统再负责候选 Skill 的比较、修订和回滚。

### 方向 D：TaskLens Regression（P2）

**目标**：把单次观测扩展为小型回归中心。

- 任务集合与标签；
- 本地批量执行；
- 基线版本和当前版本比较；
- 失败分类聚合；
- CI 输出 JUnit/Markdown 报告。

它的市场和测试开发价值更强，但需要任务配置、调度、并发和报告协议，不建议作为九天首个切片。

## 6. 推荐的最终产品形态

```text
P0  单 Agent Runtime：TaskSpec + Browser Adapter + 断言
        |
P0  执行证据层：RunRecord + StepRecord + SQLite + Dashboard
        |
P1  复盘层：Replay、首次偏离、前后运行 Diff
        |
P2  回归层：任务集、批量执行、CI 报告、趋势统计
```

不要一开始同时做账号系统、云端数据库、复杂图表、LLM 根因分析和自动修复。那会把项目从“有明确工程问题的二开”变成“功能很多但无法验证的 Demo”。

## 7. 九天内应交付什么

### 必须交付

- 一份清晰的 TaskSpec、断言和运行记录 schema；
- 单 Agent 成功、异常、断言失败、超时四类记录；
- 脱敏与长度限制测试；
- 本地 FastAPI 任务/API 接口；
- 一个能展示空状态、成功列表、失败详情的页面；
- Playwright/真实浏览器冒烟；
- SQLite schema、Docker 启动和 Fake Provider 测试模式；
- README、架构图、API 示例和演示脚本。

### 可以延期

- 复杂图表；
- Docker Compose 多服务；
- LLM 自动分析；
- 云端同步；
- 账号权限；
- 跨机器并发调度。

## 8. 简历叙事建议

项目可以这样讲：

> 基于成熟的真实浏览器执行项目，设计并实现 TaskLens 单 Agent 浏览器测试平台，将任务理解、页面观察、动作执行和业务断言串成闭环；通过 Pydantic/SQLite 运行证据、FastAPI 和响应式页面展示失败步骤、断言结果、helper 耗时和浏览器后端差异，并以 fail-open 策略保证观测异常不影响原任务。

面试时重点讲三个取舍：

1. 为什么选择增量补齐证据层，而不是重写浏览器控制；
2. 为什么首版选择 SQLite + repository 接口，而不是直接引入云端数据库；
3. 如何把单 Agent 的步数/超时/重试预算、敏感信息、失败语义和回归验证纳入设计，而不是只做一个能显示数据的页面。
