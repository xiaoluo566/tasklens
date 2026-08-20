# TaskLens 架构设计（单 Agent 版本）

> 当前只冻结架构，不创建实现模块。代码路径和接口均属于开发前契约。

## 1. 设计目标与约束

### 目标

- 在不改变上游浏览器控制行为的前提下收集运行证据；
- 用单 Agent Runtime 完成任务理解、观察、动作、校验和有界重试闭环；
- 用 FastAPI、Pydantic v2 和 SQLite 实现可运行的本地 MVP；
- 让数据层、HTTP 层和页面层可以分别测试、替换和回退；
- 默认 fail-open：观测失败不阻断主任务。

### 约束

- Python `>=3.11`，复用现有 `paths.config_dir()`；
- 不新增云端服务和账号系统；
- 默认只绑定 `127.0.0.1`；
- 不保存完整 stdin 脚本、Cookie、页面 HTML 或截图内容；
- 记录和 API 均必须有长度/数量上限。

## 2. 拟议模块边界

```text
src/browser_harness/
├── run.py                 # 既有 CLI；接入任务启动与旁路 hook
├── agent_runtime.py       # 计划新增：单 Agent 状态机和动作校验循环
├── browser_adapter.py     # 计划新增：Browser Harness/CDP 适配层
├── observability.py       # 计划新增：脱敏、Pydantic 模型和证据构建
├── storage.py             # 计划新增：SQLite repository 和迁移边界
└── dashboard.py           # 计划新增：FastAPI 路由、页面资源、CLI serve
```

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| `run.py` | 捕获开始/结束、退出码、已有 helper trace、启动任务 | 数据库细节、HTML 拼接 |
| `agent_runtime.py` | 单 Agent 观察—动作—校验循环、步数/超时/重试预算 | 多 Agent 编排、CDP 底层连接 |
| `browser_adapter.py` | 将 daemon、Tab、DOM 和 helper 统一为动作接口 | 任务规划、历史查询 |
| `observability.py` | Pydantic 记录模型、脱敏、证据构建 | 浏览器连接、网络服务 |
| `storage.py` | SQLite schema、追加、过滤、汇总、迁移 | 页面展示、模型推理 |
| `dashboard.py` | FastAPI API、静态页面、监听参数 | 改写历史、身份系统 |

## 3. 数据流与生命周期

```text
T0  API/CLI 接收自然语言目标和 TaskSpec
T1  agent_runtime 创建单 Agent 上下文和步骤预算
T2  browser_adapter 观察页面并执行 helper 动作
T3  runtime 校验页面状态/业务断言，必要时进行有界重试
T4  observability 构建脱敏 RunRecord/StepRecord
T5  storage 事务写入 SQLite（失败则旁路降级）
T6  dashboard 通过 FastAPI 读取并展示运行证据
```

`SystemExit`、未捕获异常和正常返回必须分别覆盖；单 Agent 的最大步数、总超时和幂等重试必须可配置；Dashboard 命令本身不应生成脚本运行记录。

## 4. 运行记录契约

```json
{
  "id": "uuid",
  "started_at": "ISO-8601 UTC",
  "finished_at": "ISO-8601 UTC",
  "duration_seconds": 3.12,
  "status": "success | failed | timeout | cancelled",
  "failure_kind": "none | environment | connection | action | assertion | timeout | model | unknown",
  "goal": "redacted short goal",
  "browser_backend": "local | cdp | cloud | unknown",
  "task_preview": "redacted short summary",
  "task_length": 284,
  "exit_code": 0,
  "output_tail": "redacted bounded tail",
  "output_length": 53,
  "error": null,
  "step_count": 4,
  "steps": [
    {
      "helper": "goto",
      "observation_summary": "redacted bounded page state",
      "action": "click",
      "args": "redacted bounded args",
      "duration_seconds": 0.8,
      "status": "success | failed",
      "assertions": [{"name": "element-visible", "status": "passed", "evidence": "bounded-ref"}],
      "error": null
    }
  ]
}
```

### 限制建议

- task 摘要：最多 240 字符；
- stdout/stderr：最多 2,000 字符；
- 单步参数/错误：最多 300/1,000 字符；
- 单次运行步骤：最多 500；
- 历史记录：默认最多 10,000 条；
- API 列表：单次最多 100 条。

SQLite 表至少包含 `runs`、`steps`、`assertions` 三张表，使用 `run_id` 外键关联；所有用户输入在 Pydantic 校验后才进入 repository。

## 5. 脱敏策略

### 必须处理

- URL：去掉 query string 和 fragment，保留 scheme、host、path；
- 键名：`token`、`password`、`cookie`、`authorization`、`api_key`、`secret`、`session` 等替换为 `[REDACTED]`；
- 输出/异常：先脱敏再截断；
- 任意未知对象：转字符串前不执行其自定义副作用。

脱敏不是安全边界的唯一保障。默认回环监听、无远程写接口、不上传任务内容和用户主动关闭记录仍是必要约束。

## 6. HTTP API 契约

统一返回：

```json
{"success": true, "data": {}, "error": null, "meta": {}}
```

| 方法 | 路径 | 状态码 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/tasks` | 202/400 | 校验 TaskSpec 并启动一次单 Agent 任务 |
| GET | `/` | 200 | 自包含 HTML/CSS/JS |
| GET | `/api/health` | 200 | 服务与存储可用性 |
| GET | `/api/summary` | 200 | 总量、成功率、平均耗时、浏览器分布 |
| GET | `/api/runs` | 200/400 | 列表、limit/status/browser 过滤 |
| GET | `/api/runs/{id}` | 200/404 | 单次详情 |

除本地受校验的 `/api/tasks` 外，不提供修改历史的 POST、PUT、DELETE；Dashboard 本身保持只读，任务执行由单 Agent Runtime 负责。

## 7. 部署与进程模型

- 首版：单进程 FastAPI + Uvicorn，前端为轻量静态资源；
- 监听：默认 `127.0.0.1:8765`；
- Docker：提供非特权镜像，浏览器连接通过显式 CDP 地址注入；
- 云服务器：若显式开放端口，必须提示反向代理、访问控制和敏感数据风险；
- 停止：Ctrl+C 只停止 Dashboard，不触碰 Harness daemon。

## 8. 失败语义与回退

| 场景 | 主脚本 | Dashboard/API |
| --- | --- | --- |
| 记录目录不可写 | 保持原退出码 | 显示存储错误或空数据 |
| SQLite 查询/事务失败 | 不受影响 | health 标记存储异常并保留内存摘要 |
| 端口占用 | 不受影响 | 命令返回清晰错误 |
| API 参数非法 | 不受影响 | 返回 400 envelope |
| 不存在运行 ID | 不受影响 | 返回 404 envelope |

## 9. 上游同步策略

1. `upstream` 始终指向官方仓库；
2. 二开提交集中在独立分支，避免直接改上游 `main`；
3. 每次同步前先运行上游测试，再处理冲突；
4. 不修改上游核心 helper 契约，必要时用适配层隔离变化。
