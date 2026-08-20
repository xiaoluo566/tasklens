# TaskLens 九天开发路线图（单 Agent 版本）

> 当前进度：**Day 1 设计文档完成；Day 2 之后尚未开始**。

## 阶段 0：仓库与边界（已完成）

- 建立独立的 TaskLens 个人仓库，并引入 `browser-use/browser-harness` 作为浏览器执行基线；
- 保留 `upstream` 远端用于同步开源依赖，个人仓库使用独立名称 `tasklens`；
- 创建 `feat/observability-dashboard` 分支；
- 冻结“本地观测台”方向；
- 完成本目录中的 PRD、架构、需求和项目简介；
- 不在设计阶段提交功能实现代码。

## Day 2：单 Agent 契约与测试设计

- 先写 `TaskSpec`、断言 DSL、Runtime 状态机、脱敏规则和边界测试；
- 明确观察、动作、校验、重试、成功、SystemExit、异常和超时生命周期；
- 固定模型 Provider 接口，提供确定性 Fake Provider；
- 交付：单 Agent 测试计划、数据契约评审记录。

## Day 3：单 Agent Runtime 与本地存储 P0

- 实现 `agent_runtime.py` 的观察—动作—校验循环和有界重试；
- 实现 Pydantic 模型、SQLite schema 和 repository；
- 完成运行/步骤/断言追加、读取、过滤、汇总和限长；
- 单元测试覆盖率达到 80% 以上；
- 交付：可独立测试的 Runtime 与存储模块。

## Day 4：Browser Adapter 与运行链路接入

- 封装 daemon、Tab、DOM 和 helper 为 Browser Adapter；
- 以最小 diff 接入 `run.py`；
- 保证观测失败不影响原脚本；
- 运行上游测试并记录基线差异；
- 交付：成功/失败/异常/断言失败记录闭环。

## Day 5：FastAPI 服务层

- 实现任务启动、health、summary、runs、detail；
- 统一 envelope、参数校验和错误码；
- 完成 API 集成测试；
- 交付：本地 FastAPI 契约与示例响应。

## Day 6：Dashboard 首屏

- 实现自包含页面、总览和运行列表；
- 先完成空状态、错误状态和加载状态；
- 保持无重型前端依赖；
- 交付：桌面端 P0 页面。

## Day 7：详情回放与真实验证

- 加入失败详情、断言结果、步骤耗时、筛选和步骤回放；
- 使用真实浏览器完成桌面/375px 冒烟；
- 保存截图和测试日志作为项目证据；
- 交付：E2E 冒烟证据。

## Day 8：安全与工程化

- 做敏感字段审查、输入校验和依赖审查；
- 完成 README、CHANGELOG、Docker 可选说明；
- 做覆盖率、静态检查和全量回归；
- 交付：可发布候选版本。

## Day 9：作品集交付

- 整理架构决策、演示脚本和简历项目描述；
- 使用 Conventional Commits 提交；
- 推送个人仓库并确认 README、截图和文档链接；
- 交付：可面试演示的 P0 版本。

## 里程碑与回退点

| 里程碑 | 必须满足 | 回退点 |
| --- | --- | --- |
| M1 设计冻结 | PRD/架构/需求无矛盾 | 只保留文档，不写代码 |
| M2 数据层 | 脱敏和存储测试通过 | 不接入 `run.py` |
| M3 API | API 错误语义稳定 | 页面暂不开发 |
| M4 页面 | E2E 冒烟通过 | 保留 API，关闭页面入口 |
| M5 交付 | 全量回归和安全检查通过 | 回退到最近稳定 tag |

## 明确不做

多 Agent 协作、账号系统、云端多租户、跨机器实时协作、模型根因分析和自动修复不进入本轮九天范围；SQLite、FastAPI 和单 Agent Runtime 属于本轮核心交付。
