# Agent Handoff Metrics Bootstrap Design

[Back to README](../README.md) | [中文设计文档](design_zh.md)

This document covers the detailed architecture behind Agent Handoff Metrics Bootstrap: project memory, handoff workflow, runtime collection, data privacy boundaries, metrics modeling, and Git closure.

## Architecture

AI handoff metrics are not just token accounting. This system combines how AI coding agents hand off project context with how each AI-assisted turn becomes auditable project-level usage data.

- Handoff: keep the next AI coding agent aligned through `.agent/context.md`, `.agent/handoff.md`, `.agent/workflow.md`, and thin `AGENTS.md` / `CLAUDE.md` adapters.
- Metrics: capture each AI-assisted turn, summarize safe project-level usage, and derive local cost/value reports without committing raw prompts or local machine details.

The architecture has five planes:

- Project handoff plane: durable context, current handoff, workflow rules, and start/finish prompts.
- Runtime event plane: Codex `UserPromptSubmit` and `Stop` hooks.
- Collection plane: `.agent/scripts/agent-usage-hook.py`, token transcript reader, Git snapshot reader, and task metadata writer.
- Storage plane: local raw records, local full summary, and commit-safe public summary.
- Reporting plane: derived value report using current pricing and labor assumptions.

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
    CodexConfig[".codex/config.toml<br/>.codex/hooks.json"]
    RuntimeHook[".agent/scripts/agent-usage-hook.py"]
    AgentScripts["agent-start.sh<br/>agent-finish.sh<br/>agent-identity.sh"]
    GitHooks[".githooks/pre-commit"]
    UsageStore[".agent/usage/"]
  end

  subgraph RuntimeData["Usage data layers"]
    Pending["pending/*.json<br/>turn start snapshot"]
    RawRecords["codex-turns.jsonl<br/>local raw records"]
    FullSummary["summary.json<br/>local full summary"]
    PublicSummary["project-summary.json<br/>commit-safe summary"]
    ValueReport["value-report.json<br/>derived local report"]
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

## Deployment Flow

The deployer is conservative: existing generated files are skipped by default, and `--force` creates backups before overwriting.

```mermaid
flowchart TD
  A["Run deploy_agent_system.py"] --> B["Resolve target repo root"]
  B --> C["Choose project display name and config key"]
  C --> D["Write .agent handoff docs and prompts"]
  D --> E["Copy agent-usage-hook.py into .agent/scripts/"]
  E --> F["Write .codex hooks and Codex prompt wrappers"]
  F --> G["Write AGENTS.md and CLAUDE.md thin adapters"]
  G --> H["Write .githooks/pre-commit"]
  H --> I{"--strict-commit-msg?"}
  I -- yes --> J["Write .githooks/commit-msg"]
  I -- no --> K["Skip commit-msg hook"]
  J --> L["Append runtime ignore rules"]
  K --> L
  L --> M["Rebuild .agent/usage/project-summary.json"]
  M --> N{"--agent codex|claude?"}
  N -- yes --> O["Set repo-local AI Git identity"]
  N -- none --> P["Leave Git identity unchanged"]
  O --> Q["Print JSON deployment report"]
  P --> Q
```

## Runtime Collection Flow

Codex hooks call the same script twice per turn. The first call records a start snapshot; the second call closes the turn, computes deltas, appends a raw record, and rebuilds summaries.

```mermaid
sequenceDiagram
  participant User
  participant Codex
  participant Hooks as .codex/hooks.json
  participant Script as agent-usage-hook.py
  participant Transcript as Codex transcript
  participant Pending as pending/*.json
  participant Records as codex-turns.jsonl
  participant Summary as project-summary.json

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
  Script->>Summary: Rebuild commit-safe public summary
  Script->>Pending: Remove closed pending file
  Script-->>Hooks: Continue and suppress hook output
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
  SummaryRebuilt --> PublicSummaryWritten: write project-summary.json
  PublicSummaryWritten --> ValueReportGenerated: --write-value-report
  PublicSummaryWritten --> [*]
  ValueReportGenerated --> [*]
```

## Data And Privacy Flow

The public summary is intentionally smaller than the local records. This lets teams track AI handoff and usage in Git without exposing prompts, assistant output, transcript paths, or local session identifiers.

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
  FullFile -->|"sanitize and aggregate"| PublicFile
  PublicFile -->|"derive with current policy"| ValueFile
  UsageReadme -.-> PublicFile
```

## Metrics Model

The project summary is meant to answer operational questions without leaking sensitive content:

- `recorded_turns`: number of closed Codex turns recorded for the project.
- `assisted_tasks_estimate`: count of turns with assistant output.
- `git_closed_loops`: turns where the worktree was clean and a commit happened after the prompt started.
- `token_totals`: input, cached input, uncached input, output, reasoning output, and total token totals.
- `elapsed_seconds_total`: total recorded wall-clock time across turns.
- `complexity_counts`: AI-assessed task complexity distribution.
- `turns_by_model`: recorded turns grouped by model.
- `task_history`: sanitized AI task summaries, complexity, timing, token usage, and Git closure state.

Derived value reports add policy-dependent numbers:

- AI cost from model pricing and token totals.
- Traditional engineering cost from configured complexity-to-hours assumptions.
- Replacement savings and ROI.
- Per-model cost and value totals.

Because prices, exchange rates, and labor assumptions can change, `value-report.json` is regenerated locally and ignored by default.

## Git Closure Flow

Git closure connects usage metrics to actual repository outcomes. The hook records the starting `HEAD` and status at prompt time, then checks the final `HEAD`, status, and latest commit at stop time.

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
  H --> I["Public summary records commit subject, author, SHA, and closure state"]
  F --> J["Public summary records status counts and sample lines"]
```
