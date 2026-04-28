# Agent Handoff Metrics Bootstrap

Project memory, handoff workflow, and ROI metrics for AI coding agents.

> Make AI coding agents resume, verify, and report their work like real engineering teammates.

Chinese documentation: [README_zh.md](README_zh.md)

![HandoffKit overview: project memory and delivery metrics for AI coding agents](docs/diagrams/handoffkit-overview.svg)

`agent-handoff-metrics-bootstrap` helps teams make AI coding-agent work resumable, auditable, and measurable.

If you have switched laptops, changed coding-agent tools, or handed work to another engineer and watched the next AI session start cold, this repository targets that gap. It also addresses the harder management question: AI helped, but where is the delivery evidence and value report?

It deploys project-local memory, handoff prompts, thin agent adapters, Git identity checks, Codex hooks, and usage/value reporting into an existing repository.

After deployment, Codex, Claude Code, Cursor, Gemini CLI, or other coding agents can:

- resume work across machines, tools, and sessions;
- read durable project memory before changing code;
- write clear handoff notes after each task;
- record AI-assisted turns as safe project-level usage metrics;
- connect AI work to Git outcomes, cost, and ROI reports.

Automated turn capture is currently implemented through Codex hooks. The project memory and handoff workflow are tool-agnostic and can be used by other coding agents.

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
python3 -m py_compile .agent/scripts/agent-usage-hook.py
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --print-value-report >/tmp/agent-value-report.json
git diff --check
```

## What Gets Installed

Running the deployer in a target repository can create:

- `.agent/context.md`, `.agent/handoff.md`, `.agent/workflow.md`
- `.agent/prompts/start.md`, `.agent/prompts/finish.md`
- `.agent/scripts/agent-start.sh`, `agent-finish.sh`, `agent-identity.sh`, `agent-usage-hook.py`
- `.agent/usage/README.md` and regenerated `project-summary.json`
- `.codex/hooks.json`, `.codex/config.toml`, `.codex/prompts/*`, `.codex/scripts/*`
- Thin `AGENTS.md` and `CLAUDE.md` adapters
- `.githooks/pre-commit`, and optionally `.githooks/commit-msg`
- `.gitignore` entries for local agent and Codex runtime files

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

Commit-safe output:

- `.agent/usage/project-summary.json`: stable summary metadata only, including model, token usage, elapsed time, AI task summary, AI complexity, and Git closure state.

Ignored runtime output:

- `codex-turns.jsonl`: detailed local turn records.
- `summary.json`: local full summary with machine-specific details.
- `value-report.json`: derived cost, traditional cost, savings, and ROI report.
- pending files, lock files, and hook error logs.

Before finalizing an AI-authored task, write task metadata:

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
python3 -m py_compile "$tmp_repo/.agent/scripts/agent-usage-hook.py"
python3 "$tmp_repo/.agent/scripts/agent-usage-hook.py" --rebuild-summary
```

## Privacy Boundary

`project-summary.json` is intentionally limited to stable, reviewable metadata. It must not contain raw prompts, assistant outputs, session IDs, transcript paths, local paths, costs, ROI, API keys, tokens, or secrets.

Detailed local usage files are useful for local analysis, but they are ignored by default and should not be committed.
