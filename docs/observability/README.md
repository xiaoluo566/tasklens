# TaskLens 二开设计文档

> 当前状态：**仅完成开发前设计，尚未实现二开功能**。本文档是后续开发的约束与验收基线，不代表 Dashboard、API 或运行记录已经存在。

## 项目定位

TaskLens 是基于 [browser-use/browser-harness](https://github.com/browser-use/browser-harness) 的个人二次开发项目，目标是为 AI 浏览器自动化任务补齐“运行可观测、失败可复盘、性能可比较”的本地工作台。

- 个人仓库：`xiaoluo566/tasklens`
- 上游仓库：`browser-use/browser-harness`
- 当前工作分支：`feat/observability-dashboard`
- 当前阶段：设计冻结，暂不提交实现代码

## 为什么做这个二开

上游 Harness 已经解决了“让 Agent 操作真实浏览器”的问题，但一次任务结束后，关键信息主要停留在终端：

1. 很难快速找到最近失败的任务和失败步骤；
2. helper 的耗时没有跨任务的历史比较；
3. Agent 调试证据与测试开发的回归证据没有统一入口。

TaskLens 不重新实现浏览器控制，而是在原有执行链路上增加单 Agent Runtime、业务断言、本地运行历史、FastAPI 和 Dashboard。

## 文档索引

- [PROJECT_BRIEF.md](PROJECT_BRIEF.md)：项目简介、简历表述和面试叙事
- [RESUME_DRAFT.md](RESUME_DRAFT.md)：可直接投递的项目成稿、面试口径和兑现清单
- [SECONDARY_DEVELOPMENT_SPEC.md](SECONDARY_DEVELOPMENT_SPEC.md)：单 Agent 能力、接口、约束和证据映射
- [PROJECT_ANALYSIS.md](PROJECT_ANALYSIS.md)：上游项目全景、短板和二开路线
- [PRD.md](PRD.md)：问题、用户、方案、指标和边界
- [ARCHITECTURE.md](ARCHITECTURE.md)：拟议模块、数据流、安全和部署
- [REQUIREMENTS.md](REQUIREMENTS.md)：可验收需求、优先级和测试契约
- [ROADMAP.md](ROADMAP.md)：九天开发计划、里程碑和 Definition of Done

## 计划中的使用方式

以下命令是设计目标，当前版本尚未提供：

```powershell
./browser-harness task --goal "完成结算并确认订单状态"
./browser-harness dashboard
```

计划默认监听 `127.0.0.1:8765`，记录位置为 `<BH_CONFIG_DIR>/observability/runs.jsonl`。真正开始编码前，必须先完成需求评审、确认命名和安全边界，并在独立功能分支上按路线图执行 TDD。

## 设计原则

- **旁路增强**：观测失败不能改变原 Harness 的脚本输出和退出码。
- **本地优先**：默认不上传任务内容，不引入账号、多租户或云端数据库。
- **证据优先**：每个指标都能回溯到一次运行记录和 helper 步骤。
- **最小依赖**：首版使用 FastAPI、Pydantic v2 和 SQLite，避免引入云端数据库和大型前端构建链。
- **可撤回**：二开功能应可独立关闭、删除或回退，不改变上游核心 API。
