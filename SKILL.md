---
name: agent-handoff-metrics-bootstrap
description: Bootstrap project memory, handoff workflow, and usage/ROI metrics for AI coding agents. Use when the user asks to install or bootstrap `.agent` handoff docs, thin `AGENTS.md`/`CLAUDE.md` adapters, Codex hooks, project-local AI usage tracking, task metadata, token summaries, derived cost/ROI reporting, or says phrases like "请根据这个 skill 在本工程中部署", "部署 AI 接管", "集成 AI 度量", or "把这套 agent workflow 用到当前项目".
---

# Agent Handoff Metrics Bootstrap

Project memory, handoff workflow, and ROI metrics for AI coding agents.

Use this skill to install a portable `.agent` single-source-of-truth handoff system plus Codex usage metrics into the current repository.

## Workflow

1. Resolve the target repo root with `git rev-parse --show-toplevel` or use the current directory.
2. Run the bundled deploy script:

```bash
python3 /root/.codex/skills/agent-handoff-metrics-bootstrap/scripts/deploy_agent_system.py --repo "$PWD"
```

Useful options:

```bash
--project-name "Project Name"   # customize generated headings
--agent codex|claude|none       # default codex; sets repo-local Git identity
--force                         # overwrite after backing up existing files
--strict-commit-msg             # install a basic Conventional Commits hook
```

3. Inspect the script report and `git status --short`.
4. If files already existed and were skipped, merge the generated pattern manually or rerun with `--force` only after deciding backups are acceptable.
5. Customize `.agent/context.md`, `.agent/handoff.md`, and `.agent/workflow.md` for the project. Keep durable facts in `.agent/*`; keep `AGENTS.md` and `CLAUDE.md` thin.
6. Validate:

```bash
python3 -m py_compile .agent/scripts/agent-usage-hook.py
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --print-value-report >/tmp/agent-value-report.json
git diff --check
```

## What Gets Deployed

- `.agent/context.md`, `.agent/handoff.md`, `.agent/workflow.md`
- `.agent/prompts/start.md`, `.agent/prompts/finish.md`
- `.agent/scripts/agent-start.sh`, `agent-finish.sh`, `agent-identity.sh`, `agent-usage-hook.py`
- `.agent/usage/README.md` and a regenerated `project-summary.json`
- `.codex/hooks.json`, `.codex/config.toml` hook enablement, `.codex/prompts/*`, `.codex/scripts/*`
- Thin `AGENTS.md` and `CLAUDE.md` entry adapters
- `.githooks/pre-commit` identity check
- `.gitignore` entries for local `.agent`/`.codex` runtime files

The deploy script does not commit anything.

## Usage Metrics Policy

- `project-summary.json` is commit-safe and should contain stable metadata only: model, token usage, elapsed time, AI task summary, AI complexity, and Git closure state.
- Do not put raw user prompts, assistant outputs, session IDs, transcript paths, local paths, costs, or ROI in `project-summary.json`.
- Cost, traditional cost, savings, and ROI are derived reports. Generate them from the current summary and current unit policy:

```bash
python3 .agent/scripts/agent-usage-hook.py --write-value-report
```

- `value-report.json` is ignored by default because prices, exchange rates, and labor assumptions can change.

## Agent Metadata Requirement

Before finalizing a task in a deployed repo, write AI-authored task metadata:

```bash
python3 .agent/scripts/agent-usage-hook.py --set-current-turn-metadata \
  --description "本轮任务摘要" \
  --complexity low|medium|high \
  --reason "AI 评估复杂度的依据"
```

Complexity must be AI-assessed. Do not infer complexity from token count, elapsed time, or changed-file count. If metadata is missing, the hook records complexity as `unknown`.

## Notes

- The deploy script is intentionally conservative: existing files are skipped unless `--force` is passed.
- If a repository already has project-specific rules, integrate them into `.agent/workflow.md` after deployment.
- If the user asks to apply this system to another repo, run the script there, then immediately review generated `.agent/*` for project-specific corrections.
