# Agent Handoff Metrics Bootstrap Design

[Back to README](../README.md) | [中文设计文档](design_zh.md)

This document covers the detailed architecture behind Agent Handoff Metrics Bootstrap: project memory, handoff workflow, runtime collection, data privacy boundaries, metrics modeling, and Git closure.

## Architecture

AI handoff metrics are not just token accounting. This system combines how AI coding agents hand off project context with how each AI-assisted turn becomes auditable project-level usage data.

- Handoff: keep the next AI coding agent aligned through `.agent/context.md`, `.agent/handoff.md`, `.agent/workflow.md`, thin `AGENTS.md` / `CLAUDE.md` adapters, and Codex compatibility pointers back to `.agent/*`.
- Metrics: capture each AI-assisted turn as local hook-layer audit data, then let a background Codex maintainer update a commit-safe task-level value summary. Cost and ROI are derived later without committing raw prompts or local machine details.

The architecture has six planes:

- Project handoff plane: durable context, current handoff, workflow rules, and start/finish prompts.
- Runtime event plane: Codex `UserPromptSubmit` and `Stop` hooks.
- Collection plane: `.agent/scripts/agent-usage-hook.py`, token transcript reader, Git snapshot reader, and local task metadata writer.
- Maintainer plane: `.agent/scripts/project-summary-maintainer.sh` runs Codex after Stop to maintain `project-summary.json`.
- Storage plane: local raw records, local audit summary, and commit-safe task-level public summary.
- Reporting plane: derived value report using the current task summary, pricing, and labor assumptions.

```mermaid
flowchart LR
  subgraph SkillRepo["This skill repository"]
    Skill["SKILL.md"]
    Deployer["scripts/deploy_agent_system.py"]
    HookAsset["assets/agent-usage-hook.py"]
  end

  subgraph TargetRepo["Target repository after deployment"]
    AgentDocs[".agent/context.md<br/>.agent/handoff.md<br/>.agent/workflow.md"]
    EntryAdapters["AGENTS.md<br/>CLAUDE.md"]
    CodexConfig[".codex/config.toml<br/>.codex/hooks.json<br/>.codex/context.md<br/>.codex/handoff.md"]
    ReadmeEntry["README handoff entry"]
    RuntimeHook[".agent/scripts/agent-usage-hook.py"]
    Maintainer["project-summary-maintainer.sh<br/>maintain-project-summary.md"]
    AgentScripts["agent-start.sh<br/>agent-finish.sh<br/>agent-identity.sh"]
    GitHooks[".githooks/pre-commit"]
    UsageStore[".agent/usage/"]
  end

  subgraph RuntimeData["Usage data layers"]
    Pending["pending/*.json<br/>turn start snapshot"]
    RawRecords["codex-turns.jsonl<br/>local raw records"]
    FullSummary["summary.json<br/>local hook audit summary"]
    PublicSummary["project-summary.json<br/>AI-maintained task value summary"]
    ValueReport["value-report.json<br/>derived local report"]
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

## Deployment Flow

The deployer is conservative: existing generated files are skipped by default, and `--force` creates backups before overwriting.

```mermaid
flowchart TD
  A["Run deploy_agent_system.py"] --> B["Resolve target repo root"]
  B --> C["Choose project display name and config key"]
  C --> D["Write .agent handoff docs and prompts"]
  D --> E["Copy agent-usage-hook.py and write project-summary maintainer"]
  E --> F["Write .codex hooks, prompt wrappers, and compatibility pointers"]
  F --> G["Write AGENTS.md and CLAUDE.md thin adapters"]
  G --> H["Write .githooks/pre-commit"]
  H --> I{"--strict-commit-msg?"}
  I -- yes --> J["Write .githooks/commit-msg"]
  I -- no --> K["Skip commit-msg hook"]
  J --> L["Append runtime ignore rules and README handoff entry"]
  K --> L
  L --> M["Rebuild local audit summary and initialize project-summary.json"]
  M --> N{"--agent codex|claude?"}
  N -- yes --> O["Set repo-local AI Git identity"]
  N -- none --> P["Leave Git identity unchanged"]
  O --> Q["Print JSON deployment report"]
  P --> Q
```

## Runtime Collection Flow

Codex hooks call the same script twice per turn. The first call records a start snapshot; the second call closes the turn, computes deltas, appends a raw local audit record, rebuilds the ignored local audit summary, and queues the background project-summary maintainer.

```mermaid
sequenceDiagram
  participant User
  participant Codex
  participant Hooks as .codex/hooks.json
  participant Script as agent-usage-hook.py
  participant Transcript as Codex transcript
  participant Pending as pending/*.json
  participant Records as codex-turns.jsonl
  participant LocalSummary as summary.json
  participant Maintainer as background Codex maintainer
  participant ProjectSummary as project-summary.json

  User->>Codex: Submit prompt
  Codex->>Hooks: UserPromptSubmit event
  Hooks->>Script: event JSON
  Script->>Transcript: Read latest token_count snapshot
  Script->>Script: Estimate prompt tokens and capture Git start state
  Script->>Pending: Write session_id + turn_id pending file
  Script-->>Hooks: Continue without blocking Codex

  Codex->>User: Return assistant result
  Codex->>Hooks: Stop event
  Hooks->>Script: event JSON with last_assistant_message
  Script->>Pending: Load pending start snapshot
  Script->>Transcript: Read final token_count snapshot
  Script->>Script: Compute turn token delta, elapsed time, Git closure, task metadata
  Script->>Records: Append local raw turn record
  Script->>LocalSummary: Rebuild ignored hook-layer audit summary
  Script->>Pending: Remove closed pending file
  Script->>Maintainer: Start background process with hooks disabled
  Script-->>Hooks: Continue and suppress hook output
  Maintainer->>Records: Read local audit records
  Maintainer->>ProjectSummary: Merge turns into task-level value summary
```

## Turn State Machine

Each turn moves through a small lifecycle. A turn can still be recorded if transcript token data is incomplete; in that case the script falls back from cumulative transcript deltas to the latest model-call usage, then to zeroed usage fields.

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

## Data And Privacy Flow

The public summary is intentionally task-level rather than turn-level. Hook files keep the local audit trail; the background maintainer decides which turns become value-bearing tasks and which turns remain only in local metadata.

```mermaid
flowchart LR
  subgraph LocalOnly["Ignored local runtime data"]
    Prompt["Raw user prompt"]
    Assistant["Raw assistant output"]
    Session["session_id / turn_id<br/>transcript_path / local paths"]
    PendingFile["pending/*.json"]
    RawFile["codex-turns.jsonl"]
    FullFile["summary.json"]
    ValueFile["value-report.json"]
    MaintainerLog["project-summary-maintainer.log"]
  end

  subgraph CommitSafe["Git-trackable data"]
    PublicFile["project-summary.json"]
    UsageReadme[".agent/usage/README.md"]
  end

  Prompt --> PendingFile
  Session --> PendingFile
  PendingFile --> RawFile
  Assistant --> RawFile
  RawFile --> FullFile
  RawFile -->|"background AI merges delivered turns"| PublicFile
  MaintainerLog -.-> RawFile
  PublicFile -->|"derive with current policy"| ValueFile
  UsageReadme -.-> PublicFile
```

## Metrics Model

The hook-layer audit summary answers runtime questions and stays local:

- recorded turns, token totals, elapsed time, models, and Git status per turn.
- local prompt/output estimates and machine-specific evidence.

The project summary answers task-value questions and is safe to track:

- `tasks[]`: task-level summaries maintained by background Codex.
- `included_turn_indexes`: the audit-record order numbers included in each task, without session IDs or turn IDs.
- `token_usage` and `elapsed_seconds`: aggregate values for the included turns.
- `business_value`: AI-maintained description, complexity, and rationale.
- `totals`: audit turn counts, included turn counts, excluded turn counts, and task counts.

Consultation, pure Git bookkeeping, hook smoke tests, and no-deliverable turns may be omitted from `tasks[]` and remain only in the hook audit files.

Derived value reports add policy-dependent numbers:

- AI cost from task-level model and token totals.
- Traditional engineering cost from configured complexity-to-hours assumptions.
- Replacement savings and ROI.
- Per-model cost and value totals.

Because prices, exchange rates, and labor assumptions can change, `value-report.json` is regenerated locally and ignored by default.

## Git Closure Flow

Git closure connects hook metadata to actual repository outcomes. The hook records the starting `HEAD` and status at prompt time, then checks the final `HEAD`, status, and latest commit at stop time. The background maintainer may roll those hints into task-level Git evidence when a turn contributed to a value-bearing task.

```mermaid
flowchart TD
  A["Prompt starts"] --> B["Capture start HEAD and status"]
  B --> C["AI edits files, runs checks, may commit"]
  C --> D["Stop hook reads final Git state"]
  D --> E{"Worktree clean?"}
  E -- no --> F["git_closed_loop = false"]
  E -- yes --> G{"Latest commit time >= turn start?"}
  G -- no --> F
  G -- yes --> H["git_closed_loop = true"]
  H --> I["Hook audit records commit subject, author, SHA, and closure state"]
  F --> J["Hook audit records status counts and sample lines"]
  I --> K["Maintainer may include commit evidence in task summary"]
```
