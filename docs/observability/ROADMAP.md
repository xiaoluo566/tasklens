# TaskLens 九天开发路线图（单 Agent 版本）

> 当前进度：**P0 核心实现完成；真实 Chrome/CDP、Docker 和交付截图待验证**。

## 阶段 0：仓库与边界（已完成）

- 建立独立的 TaskLens 个人仓库，并引入 `browser-use/browser-harness` 作为浏览器执行基线；
- 保留 `upstream` 远端用于同步开源依赖，个人仓库使用独立名称 `tasklens`；
- 创建 `feat/observability-dashboard` 分支；
- 冻结“本地观测台”方向；
- 完成本目录中的 PRD、架构、需求和项目简介；
- 不在设计阶段提交功能实现代码。

## Day 2：单 Agent 契约与测试设计（已完成）

- 先写 `TaskSpec`、断言 DSL、Runtime 状态机、脱敏规则和边界测试；
- 明确观察、动作、校验、重试、成功、SystemExit、异常和超时生命周期；
- 固定模型 Provider 接口，提供确定性 Fake Provider；
- 交付：`tasklens_domain.py`、确定性 Provider 契约和运行状态测试。

## Day 3：单 Agent Runtime 与本地存储 P0（已完成）

- 实现 `agent_runtime.py` 的观察—动作—校验循环和有界重试；
- 实现 Pydantic 模型、SQLite schema 和 repository；
- 完成运行/步骤/断言追加、读取、过滤、汇总和限长；
- 单元测试覆盖率达到 80% 以上；
- 交付：Runtime、SQLite repository、脱敏证据和 fail-open 测试。

## Day 4：Browser Adapter 与运行链路接入（P0 已完成）

- 封装 daemon、Tab、DOM 和 helper 为 Browser Adapter；
- 以独立 `tasklens_cli.py` 和组合层接入，不修改上游 `run.py`；
- 保证观测失败不影响原脚本；
- 运行上游测试并记录基线差异；
- 交付：成功/失败/异常/断言失败记录闭环；真实 Chrome 冒烟待验证。

## Day 5：FastAPI 服务层（已完成）

- 实现任务启动、health、summary、runs、detail；
- 统一 envelope、参数校验和错误码；
- 完成 API 集成测试；
- 交付：本地 FastAPI 契约、统一 envelope 和集成测试。

## Day 6：Dashboard 首屏（已完成）

- 实现自包含页面、总览和运行列表；
- 先完成空状态、错误状态和加载状态；
- 保持无重型前端依赖；
- 交付：自包含桌面端 P0 页面；窄屏实测待完成。

## Day 7：详情回放与真实验证（部分完成）

- 加入失败详情、断言结果、步骤耗时、筛选和步骤回放；
- 已完成运行详情、断言、步骤耗时排序和 Playwright 契约覆盖；真实浏览器桌面/375px 冒烟与截图待在目标环境执行。

## Day 8：安全与工程化（进行中）

- 做敏感字段审查、输入校验和依赖审查；
- 已完成 README、Provider 安全边界、静态检查和 TaskLens 回归；Docker、真实浏览器与真实模型仍待目标环境验证。全量基线当前为 257 通过、1 个上游 Windows checkout 资源链接失败。

## Day 9：作品集交付（待完成）

- 整理架构决策、演示脚本和简历项目描述；
- 使用 Conventional Commits 提交并推送；
- 推送个人仓库并确认 README、截图和文档链接；
- 交付：可面试演示的 P0 版本。

## 里程碑与回退点

| 里程碑 | 必须满足 | 回退点 |
| --- | --- | --- |
| M1 设计冻结 | PRD/架构/需求无矛盾 | 已达成 |
| M2 数据层 | 脱敏和存储测试通过 | 已达成 |
| M3 API | API 错误语义稳定 | 已达成 |
| M4 页面 | 契约测试通过；真实浏览器冒烟待补 | 暂保留 API/页面 |
| M5 交付 | 全量回归、真实环境和安全检查通过 | 回退到最近稳定 tag |

## 明确不做

多 Agent 协作、账号系统、云端多租户、跨机器实时协作、模型根因分析和自动修复不进入本轮九天范围；SQLite、FastAPI 和单 Agent Runtime 属于本轮核心交付。
