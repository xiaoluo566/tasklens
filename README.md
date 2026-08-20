# TaskLens

AI 浏览器自动化运行观测与评估平台（设计阶段）

> 当前仓库仅完成二次开发前的需求与架构设计，尚未实现 Dashboard、运行记录或新增 API。仓库中的浏览器控制能力来自上游 `browser-use/browser-harness`，不能将其误写为本项目独立实现。

## 项目简介

TaskLens 是基于 [browser-use/browser-harness](https://github.com/browser-use/browser-harness) 的个人二次开发项目，计划为 AI 浏览器任务增加本地优先的运行观测层：沉淀脱敏后的运行历史、复盘失败步骤、比较 helper 耗时，并通过只读 API 和响应式 Dashboard 展示证据。

项目不重新实现 CDP 浏览器控制。核心价值是围绕成熟 Harness 做增量设计，并处理测试开发场景中容易被忽略的失败语义、敏感数据、兼容性和可验证性。

## 当前状态

| 项目 | 状态 |
| --- | --- |
| 上游 Fork 与本地克隆 | 已完成 |
| 独立项目命名与开发分支 | 已完成 |
| PRD、架构、需求、九天路线图 | 已完成 |
| 本地运行记录与脱敏 | 未开始 |
| HTTP API 与前端 Dashboard | 未开始 |
| 自动化测试、Docker 与演示部署 | 未开始 |

## 计划能力

- 记录任务状态、退出码、耗时、浏览器类型和 helper 步骤；
- 对 URL 查询参数、token、password、cookie、Authorization 等内容脱敏；
- 提供运行汇总、失败筛选、单次详情和 helper 耗时排序；
- 使用 fail-open 旁路设计，观测失败不改变原任务退出码；
- 默认仅监听 `127.0.0.1`，不上传完整任务和页面内容；
- 通过 pytest、API 集成测试和真实浏览器 E2E 验证关键流程。

## 设计文档

- [设计入口](docs/observability/README.md)
- [项目全景分析：上游能力、短板与二开路线](docs/observability/PROJECT_ANALYSIS.md)
- [项目简介与简历边界](docs/observability/PROJECT_BRIEF.md)
- [产品需求文档](docs/observability/PRD.md)
- [架构设计](docs/observability/ARCHITECTURE.md)
- [需求与验收矩阵](docs/observability/REQUIREMENTS.md)
- [九天开发路线图](docs/observability/ROADMAP.md)

## 拟议架构

```text
browser-harness stdin script
          |
          v
   原有执行生命周期
          |
          +----> 脱敏运行记录（JSONL）
                         |
                         v
                 只读 HTTP API
                         |
                         v
               TaskLens Dashboard
```

## 开发边界

首轮不做账号系统、云端多租户、复杂数据库、模型根因分析、自动修复或跨机器实时协作。详细范围与验收标准以 [PRD](docs/observability/PRD.md) 和 [需求矩阵](docs/observability/REQUIREMENTS.md) 为准。

## 上游与许可证

- 上游项目：[`browser-use/browser-harness`](https://github.com/browser-use/browser-harness)
- 当前基线：`browser-harness 0.1.9`，上游提交 `41108b8`
- 上游许可证：[MIT](LICENSE)

本项目会保留 `upstream` 远端以同步官方更新，并将个人二开提交隔离在独立分支。
