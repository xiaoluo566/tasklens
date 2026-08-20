# HarnessScope 架构设计（拟议）

> 当前只冻结架构，不创建实现模块。代码路径和接口均属于开发前契约。

## 1. 设计目标与约束

### 目标

- 在不改变上游浏览器控制行为的前提下收集运行证据；
- 用标准库实现可运行的本地 MVP；
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
├── run.py                 # 既有 CLI；只增加旁路 hook
├── observability.py       # 计划新增：脱敏、模型、JSONL 存储
└── dashboard.py           # 计划新增：HTTP handler、页面资源、CLI serve
```

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| `run.py` | 捕获开始/结束、退出码、已有 helper trace | 数据库细节、HTML 拼接 |
| `observability.py` | 记录模型、脱敏、读写、聚合 | 浏览器连接、网络服务 |
| `dashboard.py` | 只读 API、静态页面、监听参数 | 写入运行记录、身份认证 |

## 3. 数据流与生命周期

```text
T0  run.py 读取 stdin
T1  记录开始时间（内存）
T2  原有 daemon/helper 执行
T3  捕获 stdout/stderr tail、helper trace、退出状态
T4  observability.build_record() 脱敏并限制大小
T5  RunStore.append() 追加 JSONL（失败则静默降级）
T6  dashboard 只读加载并聚合
```

`SystemExit`、未捕获异常和正常返回必须分别覆盖；Dashboard 命令本身不应生成脚本运行记录。

## 4. 运行记录契约

```json
{
  "id": "uuid",
  "started_at": "ISO-8601 UTC",
  "finished_at": "ISO-8601 UTC",
  "duration_seconds": 3.12,
  "status": "success | failed",
  "command": "script",
  "browser": "local | cdp | cloud | unknown",
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
      "args": "redacted bounded args",
      "duration_seconds": 0.8,
      "status": "success | failed",
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
| GET | `/` | 200 | 自包含 HTML/CSS/JS |
| GET | `/api/health` | 200 | 服务与存储可用性 |
| GET | `/api/summary` | 200 | 总量、成功率、平均耗时、浏览器分布 |
| GET | `/api/runs` | 200/400 | 列表、limit/status/browser 过滤 |
| GET | `/api/runs/{id}` | 200/404 | 单次详情 |

不提供 POST/PUT/DELETE，避免 Dashboard 变成任务控制入口。

## 7. 部署与进程模型

- 首版：单进程 `ThreadingHTTPServer`，前端为内嵌资源；
- 监听：默认 `127.0.0.1:8765`；
- Docker：后续可提供非特权镜像，但不把 Docker 作为首版运行前提；
- 云服务器：若显式开放端口，必须提示反向代理、访问控制和敏感数据风险；
- 停止：Ctrl+C 只停止 Dashboard，不触碰 Harness daemon。

## 8. 失败语义与回退

| 场景 | 主脚本 | Dashboard/API |
| --- | --- | --- |
| 记录目录不可写 | 保持原退出码 | 显示存储错误或空数据 |
| JSONL 有坏行 | 不受影响 | 跳过坏行并继续读取 |
| 端口占用 | 不受影响 | 命令返回清晰错误 |
| API 参数非法 | 不受影响 | 返回 400 envelope |
| 不存在运行 ID | 不受影响 | 返回 404 envelope |

## 9. 上游同步策略

1. `upstream` 始终指向官方仓库；
2. 二开提交集中在独立分支，避免直接改上游 `main`；
3. 每次同步前先运行上游测试，再处理冲突；
4. 不修改上游核心 helper 契约，必要时用适配层隔离变化。

