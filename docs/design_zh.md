# Agent Handoff Metrics Bootstrap 设计文档

[返回 README](../README_zh.md) | [English design](design.md)

本文档说明 Agent Handoff Metrics Bootstrap 的详细设计：项目记忆、交接工作流、运行时采集、数据隐私边界、度量模型和 Git 闭环。

## AI 接管与度量架构

这里的 AI 接管与度量不是单纯记录 token，而是把“AI 如何接管项目”和“每轮 AI 工作如何形成可审计度量”放在同一套工程结构里。

- Handoff：通过 `.agent/context.md`、`.agent/handoff.md`、`.agent/workflow.md`、轻量 `AGENTS.md` / `CLAUDE.md` 入口，以及指回 `.agent/*` 的 Codex 兼容指针，让下一个 AI coding agent 能稳定接手。
- Metrics：把每轮 AI 辅助工作记录为本机 hook 层审计数据，再由后台 Codex maintainer 更新可提交的任务级价值摘要；成本和 ROI 后续派生，同时避免提交原始 prompt 或本机运行细节。

整体架构分为六层：

- Project handoff plane：项目上下文、当前交接、工作规则、启动/收尾提示。
- Runtime event plane：Codex `UserPromptSubmit` 和 `Stop` hooks。
- Collection plane：`.agent/scripts/agent-usage-hook.py`、token transcript 读取、Git 快照读取、本机任务元数据写入。
- Maintainer plane：`.agent/scripts/project-summary-maintainer.sh` 在 Stop 后运行 Codex 维护 `project-summary.json`。
- Storage plane：本地原始记录、本地审计汇总、可提交任务级公开摘要。
- Reporting plane：基于当前任务摘要、价格、汇率和人工工时假设派生价值报告。

```mermaid
flowchart LR
  subgraph SkillRepo["本 skill 仓库"]
    Skill["SKILL.md"]
    Deployer["scripts/deploy_agent_system.py"]
    HookAsset["assets/agent-usage-hook.py"]
  end

  subgraph TargetRepo["部署后的目标仓库"]
    AgentDocs[".agent/context.md<br/>.agent/handoff.md<br/>.agent/workflow.md"]
    EntryAdapters["AGENTS.md<br/>CLAUDE.md"]
    CodexConfig[".codex/config.toml<br/>.codex/hooks.json<br/>.codex/context.md<br/>.codex/handoff.md"]
    ReadmeEntry["README 交接入口"]
    RuntimeHook[".agent/scripts/agent-usage-hook.py"]
    Maintainer["project-summary-maintainer.sh<br/>maintain-project-summary.md"]
    AgentScripts["agent-start.sh<br/>agent-finish.sh<br/>agent-identity.sh"]
    GitHooks[".githooks/pre-commit"]
    UsageStore[".agent/usage/"]
  end

  subgraph RuntimeData["用量数据分层"]
    Pending["pending/*.json<br/>本轮开始快照"]
    RawRecords["codex-turns.jsonl<br/>本地原始记录"]
    FullSummary["summary.json<br/>本地 hook 审计汇总"]
    PublicSummary["project-summary.json<br/>AI 维护的任务价值摘要"]
    ValueReport["value-report.json<br/>本地派生价值报告"]
  end

  Skill --> Deployer
  HookAsset --> Deployer
  Deployer --> AgentDocs
  Deployer --> EntryAdapters
  Deployer --> CodexConfig
  Deployer --> ReadmeEntry
  Deployer --> RuntimeHook
  Deployer --> Maintainer
  Deployer --> AgentScripts
  Deployer --> GitHooks
  Deployer --> UsageStore
  RuntimeHook --> Pending
  RuntimeHook --> RawRecords
  RawRecords --> FullSummary
  RuntimeHook --> Maintainer
  Maintainer --> PublicSummary
  PublicSummary --> ValueReport
```

## 部署流程

部署脚本是保守的：默认跳过已经存在的文件；传入 `--force` 时，会先备份再覆盖。

```mermaid
flowchart TD
  A["运行 deploy_agent_system.py"] --> B["解析目标仓库根目录"]
  B --> C["确定项目显示名和配置 key"]
  C --> D["写入 .agent 交接文档和提示词"]
  D --> E["复制 agent-usage-hook.py 并写入 project-summary maintainer"]
  E --> F["写入 .codex hooks、Codex prompt wrappers 和兼容指针"]
  F --> G["写入 AGENTS.md 和 CLAUDE.md 薄适配器"]
  G --> H["写入 .githooks/pre-commit"]
  H --> I{"是否启用 --strict-commit-msg?"}
  I -- 是 --> J["写入 .githooks/commit-msg"]
  I -- 否 --> K["跳过 commit-msg hook"]
  J --> L["追加运行时文件忽略规则和 README 交接入口"]
  K --> L
  L --> M["重建本机审计汇总并初始化 project-summary.json"]
  M --> N{"--agent codex|claude?"}
  N -- 是 --> O["设置仓库本地 AI Git 身份"]
  N -- none --> P["保持 Git 身份不变"]
  O --> Q["打印 JSON 部署报告"]
  P --> Q
```

## 运行时采集流程

Codex hooks 每轮会调用同一个脚本两次。第一次记录开始快照；第二次关闭本轮记录，计算用量差值、追加本机原始审计记录、重建被忽略的本机审计汇总，并排队启动后台 project-summary maintainer。

```mermaid
sequenceDiagram
  participant User as 用户
  participant Codex
  participant Hooks as .codex/hooks.json
  participant Script as agent-usage-hook.py
  participant Transcript as Codex transcript
  participant Pending as pending/*.json
  participant Records as codex-turns.jsonl
  participant LocalSummary as summary.json
  participant Maintainer as 后台 Codex maintainer
  participant ProjectSummary as project-summary.json

  User->>Codex: 提交 prompt
  Codex->>Hooks: UserPromptSubmit event
  Hooks->>Script: event JSON
  Script->>Transcript: 读取最新 token_count 快照
  Script->>Script: 估算 prompt tokens 并捕获 Git 开始状态
  Script->>Pending: 写入 session_id + turn_id pending 文件
  Script-->>Hooks: 不阻塞 Codex，继续执行

  Codex->>User: 返回 assistant 结果
  Codex->>Hooks: Stop event
  Hooks->>Script: event JSON，包含 last_assistant_message
  Script->>Pending: 读取本轮开始快照
  Script->>Transcript: 读取最终 token_count 快照
  Script->>Script: 计算 token 差值、耗时、Git 闭环、任务元数据
  Script->>Records: 追加本地原始 turn 记录
  Script->>LocalSummary: 重建被忽略的 hook 层审计汇总
  Script->>Pending: 删除已关闭 pending 文件
  Script->>Maintainer: 以禁用 hooks 的环境启动后台进程
  Script-->>Hooks: 继续执行并隐藏 hook 输出
  Maintainer->>Records: 读取本地审计记录
  Maintainer->>ProjectSummary: 合并 turns 并维护任务级价值摘要
```

## 单轮状态机

每一轮 AI 工作会经过一个小型生命周期。如果 transcript token 数据不完整，脚本仍然会记录本轮；它会先尝试使用累计 token 差值，再退回到最新一次模型调用用量，最后才使用零值字段。

```mermaid
stateDiagram-v2
  [*] --> PromptSubmitted
  PromptSubmitted --> PendingSnapshot: UserPromptSubmit hook
  PendingSnapshot --> MetadataAnnotated: --set-current-turn-metadata
  PendingSnapshot --> StopReceived: Stop hook
  MetadataAnnotated --> StopReceived: Stop hook
  StopReceived --> RawRecordWritten: append codex-turns.jsonl
  RawRecordWritten --> SummaryRebuilt: rebuild summary.json
  SummaryRebuilt --> MaintainerQueued: start project-summary maintainer
  MaintainerQueued --> PublicSummaryWritten: AI updates task-level project-summary.json
  PublicSummaryWritten --> ValueReportGenerated: --write-value-report
  PublicSummaryWritten --> [*]
  ValueReportGenerated --> [*]
```

## 数据和隐私边界

公开摘要刻意是任务级，而不是逐轮流水。hook 文件保留本机审计轨迹；后台 maintainer 判断哪些 turns 形成有价值任务，哪些 turns 只留在本地元数据中。

```mermaid
flowchart LR
  subgraph LocalOnly["默认忽略的本地运行数据"]
    Prompt["原始用户 prompt"]
    Assistant["原始 assistant 输出"]
    Session["session_id / turn_id<br/>transcript_path / 本地路径"]
    PendingFile["pending/*.json"]
    RawFile["codex-turns.jsonl"]
    FullFile["summary.json"]
    ValueFile["value-report.json"]
    MaintainerLog["project-summary-maintainer.log"]
  end

  subgraph CommitSafe["可提交到 Git 的数据"]
    PublicFile["project-summary.json"]
    UsageReadme[".agent/usage/README.md"]
  end

  Prompt --> PendingFile
  Session --> PendingFile
  PendingFile --> RawFile
  Assistant --> RawFile
  RawFile --> FullFile
  RawFile -->|"后台 AI 合并有交付的 turns"| PublicFile
  MaintainerLog -.-> RawFile
  PublicFile -->|"按当前策略派生"| ValueFile
  UsageReadme -.-> PublicFile
```

## 度量模型

hook 层审计汇总回答运行时问题，并保留在本机：

- 已记录 turns、token 汇总、耗时、模型、每轮 Git 状态。
- 本地 prompt/output 估算和机器相关证据。

`project-summary.json` 回答任务价值问题，并适合纳入 Git：

- `tasks[]`：后台 Codex 维护的任务级摘要。
- `included_turn_indexes`：每个任务包含的审计记录序号，不包含 session ID 或 turn ID。
- `token_usage` 和 `elapsed_seconds`：纳入 turns 的聚合值。
- `business_value`：AI 维护的价值说明、复杂度和依据。
- `totals`：审计 turn 数、纳入 turn 数、排除 turn 数和任务数。

咨询、纯 Git 流程、hook smoke、无交付结果等 turns 可以不进入 `tasks[]`，只保留在 hook 审计文件中。

派生价值报告会增加依赖策略的指标：

- 基于任务级模型和 token 用量估算 AI 成本。
- 基于复杂度到工时的配置估算传统人工成本。
- 替代节约额和 ROI。
- 按模型分组的成本和价值汇总。

由于价格、汇率和人工工时假设可能变化，`value-report.json` 默认在本地重新生成并被忽略，不提交。

## Git 闭环流程

Git 闭环把 hook 元数据和真实仓库结果连接起来。hook 会在 prompt 开始时记录起始 `HEAD` 和状态，在 stop 时检查最终 `HEAD`、工作区状态和最新提交。后台 maintainer 可以在某个 turn 形成有价值任务时，把这些提示归入任务级 Git 证据。

```mermaid
flowchart TD
  A["Prompt 开始"] --> B["捕获开始 HEAD 和工作区状态"]
  B --> C["AI 修改文件、运行检查、可能提交"]
  C --> D["Stop hook 读取最终 Git 状态"]
  D --> E{"工作区是否干净?"}
  E -- 否 --> F["git_closed_loop = false"]
  E -- 是 --> G{"最新提交时间 >= 本轮开始时间?"}
  G -- 否 --> F
  G -- 是 --> H["git_closed_loop = true"]
  H --> I["hook 审计记录提交 subject、author、SHA 和闭环状态"]
  F --> J["hook 审计记录状态计数和样例行"]
  I --> K["maintainer 可将提交证据纳入任务摘要"]
```
