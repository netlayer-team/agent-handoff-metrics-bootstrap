# Agent Handoff Metrics Bootstrap

Project memory, handoff workflow, and ROI metrics for AI coding agents.

> Make AI coding agents resume, verify, and report their work like real engineering teammates.

Chinese documentation: [README_zh.md](README_zh.md)

![HandoffKit overview: project memory and delivery metrics for AI coding agents](docs/diagrams/handoffkit-overview.svg)

`agent-handoff-metrics-bootstrap` helps teams make AI coding-agent work resumable, auditable, and measurable.

If you have switched laptops, changed coding-agent tools, or handed work to another engineer and watched the next AI session start cold, this repository targets that gap. It also addresses the harder management question: AI helped, but where is the delivery evidence and value report?

It deploys project-local memory, handoff prompts, thin agent adapters, Git identity checks, Codex hooks, a background task-value maintainer, and usage/value reporting into an existing repository.

After deployment, Codex, Claude Code, Cursor, Gemini CLI, or other coding agents can:

- resume work across machines, tools, and sessions;
- read durable project memory before changing code;
- write clear handoff notes after each task;
- record AI-assisted turns as local hook-layer audit metadata;
- maintain task-level business value summaries after Stop in the background;
- connect AI work to Git outcomes, cost, and ROI reports.

Automated turn capture is currently implemented through Codex hooks. After Stop, the hook queues a background Codex maintainer that updates `.agent/usage/project-summary.json` at task granularity. The project memory and handoff workflow are tool-agnostic and can be used by other coding agents.

The core idea is simple:

> Do not sync chat history. Sync project memory, handoff state, decisions, validation results, and delivery evidence.

Working short name: **HandoffKit for AI Coding Agents**.

## Documentation

The root README is intentionally short. Detailed architecture, flows, and diagrams live in `docs/`:

- [Design](docs/design.md): architecture, runtime collection flow, data boundaries, metrics model, and Git closure.
- [中文设计文档](docs/design_zh.md)：架构、运行时采集流程、数据边界、度量模型和 Git 闭环。
- [Docs index](docs/README.md)

## Quick Start

Install this repository as a local Codex skill:

```bash
mkdir -p /root/.codex/skills
git clone git@github.com:netlayer-team/agent-handoff-metrics-bootstrap.git \
  /root/.codex/skills/agent-handoff-metrics-bootstrap
```

Deploy it into a target repository:

```bash
python3 /root/.codex/skills/agent-handoff-metrics-bootstrap/scripts/deploy_agent_system.py --repo "$PWD"
```

Common options:

```bash
--project-name "Project Name"   # customize generated headings
--agent codex|claude|none       # default: codex; sets repo-local Git identity
--force                         # overwrite existing generated files after backups
--strict-commit-msg             # add a basic Conventional Commits commit-msg hook
```

Validate the generated setup:

```bash
git status --short
bash -n .agent/scripts/agent-start.sh
bash -n .agent/scripts/agent-finish.sh
bash -n .agent/scripts/project-summary-maintainer.sh
python3 -m py_compile .agent/scripts/agent-usage-hook.py
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --refresh-project-summary
python3 .agent/scripts/agent-usage-hook.py --print-value-report >/tmp/agent-value-report.json
git diff --check
```

## What Gets Installed

Running the deployer in a target repository can create:

- `.agent/context.md`, `.agent/handoff.md`, `.agent/workflow.md`
- `.agent/prompts/start.md`, `.agent/prompts/finish.md`, `generate_value_report_site.py`
- `.agent/scripts/agent-start.sh`, `agent-finish.sh`, `agent-identity.sh`, `agent-usage-hook.py`, `project-summary-maintainer.sh`
- `.agent/usage/README.md`, local hook audit summary, and initialized `project-summary.json`
- `.codex/hooks.json`, `.codex/config.toml`, `.codex/prompts/*`, `.codex/scripts/*`
- `.codex/context.md` and `.codex/handoff.md` compatibility pointers to `.agent/*`
- Thin `AGENTS.md` and `CLAUDE.md` adapters
- `.githooks/pre-commit`, and optionally `.githooks/commit-msg`
- `.gitignore` entries for local agent and Codex runtime files
- A README handoff entry when `README.md` already exists

The deploy script does not commit changes.

## Agent Workflow

The generated workflow gives each AI coding agent the same project entrypoint:

```bash
./.agent/scripts/agent-start.sh codex
./.agent/scripts/agent-finish.sh
```

`agent-start.sh` sets or checks the repo-local Git identity and prints the project start prompt. `agent-finish.sh` prints the handoff/summary checklist for the current session.

Generated identities:

- Codex: `Codex <noreply@openai.com>`
- Claude Code: `Claude <noreply@anthropic.com>`

The generated pre-commit hook checks the configured agent identity before commits, so AI-authored commits do not silently inherit a human Git identity.

## Usage And Value Metrics

The copied `agent-usage-hook.py` records Codex turn usage into `.agent/usage/`.

The data model has two layers:

- Hook layer: `codex-turns.jsonl` and `summary.json` record per-turn token usage, elapsed time, model, Git state, and local audit metadata.
- Project-summary layer: Stop queues `project-summary-maintainer.sh`, which runs Codex in the background and maintains `project-summary.json` as a task-level business value summary. Multiple turns may become one task, and consultation or no-deliverable turns may stay only in hook audit data.

Commit-safe output:

- `.agent/usage/project-summary.json`: stable task-level metadata only, including task summary, included turn indexes, aggregate token usage, elapsed time, AI task complexity, and Git closure hints.

Ignored runtime output:

- `codex-turns.jsonl`: detailed local turn records.
- `summary.json`: local hook-layer audit summary with machine-specific details.
- `value-report.json`: derived cost, traditional cost, savings, and ROI report.
- maintainer logs, pending files, lock files, and hook error logs.

Before finalizing an AI-authored task, you can write hook-layer task metadata as a hint for later summarization:

```bash
python3 .agent/scripts/agent-usage-hook.py --set-current-turn-metadata \
  --description "Task summary for this turn" \
  --complexity low|medium|high \
  --reason "Why the AI assessed this complexity level"
```

Cost and ROI reports are derived from current policy assumptions and should be regenerated instead of committed:

```bash
python3 .agent/scripts/agent-usage-hook.py --write-value-report
```

The value report is derived from `project-summary.json`, not directly from raw hook turns.

## Repository Layout

- `SKILL.md`: the Codex skill entrypoint and operating instructions.
- `scripts/deploy_agent_system.py`: the deployer that writes the reusable `.agent`, `.codex`, and `.githooks` scaffolding into a target repository.
- `assets/agent-usage-hook.py`: the Codex hook script copied into target repositories.
- `agents/openai.yaml`: agent marketplace/display metadata.
- `docs/`: detailed design notes and diagram source.
- `references/`: optional longer-form references, examples, or design notes for future versions.

## Development Checks

Run the lightweight checks before publishing changes to this skill:

```bash
python3 -m py_compile assets/agent-usage-hook.py scripts/deploy_agent_system.py
python3 scripts/deploy_agent_system.py --help
```

For a deployment smoke test:

```bash
tmp_repo="$(mktemp -d)"
git -C "$tmp_repo" init
python3 scripts/deploy_agent_system.py --repo "$tmp_repo" --project-name Smoke --agent none
bash -n "$tmp_repo/.agent/scripts/agent-start.sh"
bash -n "$tmp_repo/.agent/scripts/agent-finish.sh"
bash -n "$tmp_repo/.agent/scripts/project-summary-maintainer.sh"
python3 -m py_compile "$tmp_repo/.agent/scripts/agent-usage-hook.py"
(
  cd "$tmp_repo"
  python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
  python3 .agent/scripts/agent-usage-hook.py --refresh-project-summary
)
```

## Privacy Boundary

`project-summary.json` is intentionally limited to stable, reviewable metadata. It must not contain raw prompts, assistant outputs, session IDs, transcript paths, local paths, costs, ROI, API keys, tokens, or secrets.

Detailed local usage files are useful for local analysis, but they are ignored by default and should not be committed.
