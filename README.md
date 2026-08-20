# TaskLens

单 Agent AI 浏览器测试与运行观测平台（设计阶段）

> 当前仓库仅完成开发前的需求与架构设计，尚未实现 Dashboard、运行记录或新增 API。浏览器控制能力基于开源 `browser-use/browser-harness`，TaskLens 的个人开发范围是单 Agent Runtime、断言、证据存储、API、Dashboard 与测试体系。

## 项目简介

TaskLens 是一个面向复杂网页流程的单 Agent AI 浏览器测试与运行观测个人项目，基于 [browser-use/browser-harness](https://github.com/browser-use/browser-harness) 与 CDP 构建浏览器操作层，计划沉淀脱敏运行历史、校验业务结果、复盘失败步骤、比较 helper 耗时，并通过 FastAPI 和响应式 Dashboard 展示证据。

项目不重新实现 CDP 浏览器控制。核心价值是围绕成熟 Harness 做增量设计，并处理测试开发场景中容易被忽略的失败语义、敏感数据、兼容性和可验证性。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 个人仓库与开源依赖基线 | 已完成 |
| 独立项目命名与开发分支 | 已完成 |
| PRD、架构、需求、九天路线图 | 已完成 |
| 单 Agent Runtime、任务断言与运行记录 | 设计完成，待实现 |
| FastAPI 与前端 Dashboard | 设计完成，待实现 |
| 自动化测试、Docker 与演示部署 | 设计完成，待实现 |

## 计划能力

- 以单 Agent Runtime 完成任务理解、页面观察、动作执行、结果断言和有界重试；
- 记录任务状态、退出码、耗时、浏览器类型和 helper 步骤；
- 使用 Pydantic v2 校验任务、断言和运行结果，并以 SQLite 保存运行证据；
- 对 URL 查询参数、token、password、cookie、Authorization 等内容脱敏；
- 提供运行汇总、失败筛选、单次详情和 helper 耗时排序；
- 使用 fail-open 旁路设计，观测失败不改变原任务退出码；
- 默认仅监听 `127.0.0.1`，不上传完整任务和页面内容；
- 通过 pytest、API 集成测试和真实浏览器 E2E 验证关键流程。

## 设计文档

- [设计入口](docs/observability/README.md)
- [项目全景分析：开源能力、短板与开发路线](docs/observability/PROJECT_ANALYSIS.md)
- [项目简介与简历边界](docs/observability/PROJECT_BRIEF.md)
- [简历成稿与面试口径](docs/observability/RESUME_DRAFT.md)
- [单 Agent 项目开发能力规格](docs/observability/DEVELOPMENT_SPEC.md)
- [产品需求文档](docs/observability/PRD.md)
- [架构设计](docs/observability/ARCHITECTURE.md)
- [需求与验收矩阵](docs/observability/REQUIREMENTS.md)
- [九天开发路线图](docs/observability/ROADMAP.md)

## 拟议架构

```text
自然语言目标 / TaskSpec
          |
          v
   单 Agent Runtime
  观察 -> 动作 -> 断言
          |
          v
 Browser Adapter (CDP)
          |
          v
 脱敏运行证据（SQLite）
          |
          v
      FastAPI + Dashboard
```

## 开发边界

首轮不做多 Agent 协作、账号系统、云端多租户、模型根因分析、自动修复或跨机器实时协作；SQLite、FastAPI 和单 Agent Runtime 属于本轮核心范围。详细范围与验收标准以 [PRD](docs/observability/PRD.md) 和 [需求矩阵](docs/observability/REQUIREMENTS.md) 为准。

## 上游与许可证

- 上游项目：[`browser-use/browser-harness`](https://github.com/browser-use/browser-harness)
- 当前基线：`browser-harness 0.1.9`，上游提交 `41108b8`
- 上游许可证：[MIT](LICENSE)

本项目会保留 `upstream` 远端以同步开源依赖更新，并将 TaskLens 功能提交隔离在独立分支。
