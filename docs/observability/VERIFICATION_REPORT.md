# TaskLens 验证记录

更新时间：2026-08-23（Windows / Python 3.13）

本文只记录当前工作树中实际执行过的检查；真实 Chrome、真实模型端点、Docker 和窄屏 Playwright 仍需在目标环境补证。

## 已通过

| 检查 | 命令/范围 | 结果 |
| --- | --- | --- |
| TaskLens 回归 | `uv run pytest tests -k tasklens -q` | 118 passed |
| TaskLens 覆盖率 | 9 个新增核心模块，`pytest-cov` 分模块统计 | 总体 80%（2828 statements） |
| 静态类型 | `uv run pyright`（9 个新增核心模块） | 0 errors, 0 warnings |
| 代码规范 | `uv run ruff check`（9 个新增模块及 TaskLens 测试） | passed |
| 零网络演示 | `uv run tasklens task --demo --goal "打开演示任务"` | 成功 JSON envelope，退出码 0 |
| 安全失败 | 未配置模型变量执行普通 `task` | `runtime_unavailable`，退出码 1 |

## 全量基线

最近一次 `uv run pytest -q`：**257 passed, 1 failed**。

唯一失败是上游既有的 `tests/unit/test_skill.py::test_packaged_skill_frontmatter_is_valid_simple_yaml`：Windows checkout 下 `src/browser_harness/SKILL.md` 被还原为文本 `../../SKILL.md`，`importlib.resources` 读取不到根目录 frontmatter。该问题不由 TaskLens 模块或测试触发；本轮保留原上游资源布局，没有为了全绿改写上游 symlink。

测试过程中还会出现 Windows pytest 临时目录清理的 `WinError 5` 警告，不影响测试断言结果。

## 尚未宣称完成

- 真实 Chrome/CDP：需要在启用远程调试的测试浏览器上验证导航、点击、输入、等待、截图和断言。
- 真实 OpenAI-compatible Provider：需要使用本地环境变量和非敏感测试任务验证请求/响应契约；密钥不进入仓库。
- Playwright 375px E2E：当前有 API/HTML 契约测试，尚未保存真实浏览器截图和横向滚动检查结果。
- Docker：当前提供依赖与运行设计，尚未在目标云服务器完成镜像构建和启动实测。

因此简历中可以写已实现的 Runtime、断言、证据存储、API、Dashboard 和自动化测试；不要把上面四项的目标环境验证写成已经发生的线上事实。
