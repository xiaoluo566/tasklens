# TaskLens 演示与验证手册

本文档区分“无需外部环境即可验证的 P0 闭环”和“必须在目标机器上实测的真实浏览器/模型能力”。

## 1. 零网络演示

```powershell
uv sync --dev
uv run tasklens task --demo --goal "打开演示任务"
uv run tasklens dashboard --demo --port 8765
```

任务命令输出统一的 `success/data/error/meta` JSON；Dashboard 启动后访问 `http://127.0.0.1:8765/`。演示模式使用 `DemoModelProvider` 和内存浏览器，不连接 Chrome、不调用外部模型，适合录制项目讲解和运行 API 契约。

Dashboard 默认拒绝非回环 host。云服务器优先通过 SSH 隧道访问；若只是隔离环境临时验证，必须显式加 `--allow-remote`，并限制防火墙来源，因为当前页面没有认证和限流。

## 2. OpenAI-compatible Provider

只在明确配置端点和密钥时启用真实模型：

```powershell
$env:TASKLENS_LLM_BASE_URL = "https://your-provider.example/v1"
$env:TASKLENS_LLM_API_KEY = "<set-locally>"
$env:TASKLENS_LLM_MODEL = "your-model"
$env:TASKLENS_MODEL_PROVIDER = "openai"
uv run tasklens task --goal "检查订单状态"
```

Provider 只发送受长度限制的任务、页面摘要和步骤历史，解析 JSON action 后再经过 `normalize_action` 校验；不执行模型返回的 Python/JavaScript。API key 不写入请求体、运行记录、错误摘要或 Dashboard。

未配置必需变量时，CLI 返回 `runtime_unavailable`，这是预期的安全失败，不代表真实模型链路已经验证。

## 3. 真实 Chrome/CDP 冒烟

1. 准备专用测试 Chrome/测试账号，并按上游 `install.md` 开启 CDP；
2. 先用上游命令确认 `page_info()` 和 `ensure_real_tab()` 可用；
3. 运行 TaskLens CLI 或在组合层注入 `CDPBrowserAdapter`；
4. 用仅含公开/虚拟数据的页面验证 `goto`、`click`、`type`、`wait`、截图和断言；
5. 保存脱敏终端输出、API 响应和 Dashboard 截图，不保存 Cookie、完整页面 HTML 或真实账号信息。

此步骤尚未在所有平台完成；在简历中只能描述已实际运行过的动作和测试结果。

## 4. 交付前检查

```powershell
uv run ruff check src/browser_harness tests
uv run pyright src/browser_harness
uv run pytest -p no:cacheprovider --cov=browser_harness --cov-report=term-missing
```

上游基线测试若因 Windows symlink 语义或资源链接失败，应单独记录复现命令和原因，不要把这些环境差异写成 TaskLens 功能回归。
