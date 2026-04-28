# Agent Handoff Metrics Bootstrap 设计文档

[返回 README](../README_zh.md) | [English design](design.md)

本文档说明 Agent Handoff Metrics Bootstrap 的详细设计：项目记忆、交接工作流、运行时采集、数据隐私边界、度量模型和 Git 闭环。

## AI 接管与度量架构

这里的 AI 接管与度量不是单纯记录 token，而是把“AI 如何接管项目”和“每轮 AI 工作如何形成可审计度量”放在同一套工程结构里。

- Handoff：通过 `.agent/context.md`、`.agent/handoff.md`、`.agent/workflow.md` 和轻量 `AGENTS.md` / `CLAUDE.md` 入口，让下一个 AI coding agent 能稳定接手。
- Metrics：捕获每轮 AI 辅助工作，生成可提交的项目级用量摘要，并在本地派生成本和价值报告，同时避免提交原始 prompt 或本机运行细节。

整体架构分为五层：

- Project handoff plane：项目上下文、当前交接、工作规则、启动/收尾提示。
- Runtime event plane：Codex `UserPromptSubmit` 和 `Stop` hooks。
- Collection plane：`.agent/scripts/agent-usage-hook.py`、token transcript 读取、Git 快照读取、任务元数据写入。
- Storage plane：本地原始记录、本地完整汇总、可提交公开摘要。
- Reporting plane：基于当前价格、汇率和人工工时假设派生价值报告。

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
    CodexConfig[".codex/config.toml<br/>.codex/hooks.json"]
    RuntimeHook[".agent/scripts/agent-usage-hook.py"]
    AgentScripts["agent-start.sh<br/>agent-finish.sh<br/>agent-identity.sh"]
    GitHooks[".githooks/pre-commit"]
    UsageStore[".agent/usage/"]
  end

  subgraph RuntimeData["用量数据分层"]
    Pending["pending/*.json<br/>本轮开始快照"]
    RawRecords["codex-turns.jsonl<br/>本地原始记录"]
    FullSummary["summary.json<br/>本地完整汇总"]
    PublicSummary["project-summary.json<br/>可提交公开摘要"]
    ValueReport["value-report.json<br/>本地派生价值报告"]
  end

  Skill --> Deployer
  HookAsset --> Deployer
  Deployer --> AgentDocs
  Deployer --> EntryAdapters
  Deployer --> CodexConfig
  Deployer --> RuntimeHook
  Deployer --> AgentScripts
  Deployer --> GitHooks
  Deployer --> UsageStore
  RuntimeHook --> Pending
  RuntimeHook --> RawRecords
  RawRecords --> FullSummary
  FullSummary --> PublicSummary
  PublicSummary --> ValueReport
```

## 部署流程

部署脚本是保守的：默认跳过已经存在的文件；传入 `--force` 时，会先备份再覆盖。

```mermaid
flowchart TD
  A["运行 deploy_agent_system.py"] --> B["解析目标仓库根目录"]
  B --> C["确定项目显示名和配置 key"]
  C --> D["写入 .agent 交接文档和提示词"]
  D --> E["复制 agent-usage-hook.py 到 .agent/scripts/"]
  E --> F["写入 .codex hooks 和 Codex prompt wrappers"]
  F --> G["写入 AGENTS.md 和 CLAUDE.md 薄适配器"]
  G --> H["写入 .githooks/pre-commit"]
  H --> I{"是否启用 --strict-commit-msg?"}
  I -- 是 --> J["写入 .githooks/commit-msg"]
  I -- 否 --> K["跳过 commit-msg hook"]
  J --> L["追加运行时文件忽略规则"]
  K --> L
  L --> M["重建 .agent/usage/project-summary.json"]
  M --> N{"--agent codex|claude?"}
  N -- 是 --> O["设置仓库本地 AI Git 身份"]
  N -- none --> P["保持 Git 身份不变"]
  O --> Q["打印 JSON 部署报告"]
  P --> Q
```

## 运行时采集流程

Codex hooks 每轮会调用同一个脚本两次。第一次记录开始快照；第二次关闭本轮记录，计算用量差值、追加原始记录并重建摘要。

```mermaid
sequenceDiagram
  participant User as 用户
  participant Codex
  participant Hooks as .codex/hooks.json
  participant Script as agent-usage-hook.py
  participant Transcript as Codex transcript
  participant Pending as pending/*.json
  participant Records as codex-turns.jsonl
  participant Summary as project-summary.json

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
  Script->>Summary: 重建可提交公开摘要
  Script->>Pending: 删除已关闭 pending 文件
  Script-->>Hooks: 继续执行并隐藏 hook 输出
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
  SummaryRebuilt --> PublicSummaryWritten: write project-summary.json
  PublicSummaryWritten --> ValueReportGenerated: --write-value-report
  PublicSummaryWritten --> [*]
  ValueReportGenerated --> [*]
```

## 数据和隐私边界

公开摘要刻意比本地记录更小。这样团队可以把 AI 接管和使用量指标纳入 Git 跟踪，同时避免暴露 prompt、assistant 输出、transcript 路径或本地 session 标识。

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
  FullFile -->|"脱敏和聚合"| PublicFile
  PublicFile -->|"按当前策略派生"| ValueFile
  UsageReadme -.-> PublicFile
```

## 度量模型

`project-summary.json` 用来回答工程管理问题，同时不泄漏敏感内容：

- `recorded_turns`：已关闭并记录的 Codex turns 数量。
- `assisted_tasks_estimate`：有 assistant 输出的 turns 数量。
- `git_closed_loops`：本轮开始后产生了提交，且结束时工作区干净的 turns 数量。
- `token_totals`：input、cached input、uncached input、output、reasoning output、total token 汇总。
- `elapsed_seconds_total`：所有记录轮次的墙钟耗时总和。
- `complexity_counts`：AI 评估的任务复杂度分布。
- `turns_by_model`：按模型分组的记录轮次数。
- `task_history`：脱敏后的 AI 任务摘要、复杂度、耗时、token 用量和 Git 闭环状态。

派生价值报告会增加依赖策略的指标：

- 基于模型价格和 token 用量估算 AI 成本。
- 基于复杂度到工时的配置估算传统人工成本。
- 替代节约额和 ROI。
- 按模型分组的成本和价值汇总。

由于价格、汇率和人工工时假设可能变化，`value-report.json` 默认在本地重新生成并被忽略，不提交。

## Git 闭环流程

Git 闭环把 AI 用量指标和真实仓库结果连接起来。hook 会在 prompt 开始时记录起始 `HEAD` 和状态，在 stop 时检查最终 `HEAD`、工作区状态和最新提交。

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
  H --> I["公开摘要记录提交 subject、author、SHA 和闭环状态"]
  F --> J["公开摘要记录状态计数和样例行"]
```
