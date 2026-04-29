---
name: agent-handoff-metrics-bootstrap
description: Bootstrap project memory, handoff workflow, and usage/ROI metrics for AI coding agents. Use when the user asks to install or bootstrap `.agent` handoff docs, thin `AGENTS.md`/`CLAUDE.md` adapters, Codex hooks, project-local AI usage tracking, background task-value summary maintenance, token summaries, derived cost/ROI reporting, or says phrases like "请根据这个 skill 在本工程中部署", "部署 AI 接管", "集成 AI 度量", or "把这套 agent workflow 用到当前项目".
---

# Agent Handoff Metrics Bootstrap

Project memory, handoff workflow, and ROI metrics for AI coding agents.

Use this skill to install a portable `.agent` single-source-of-truth handoff system plus Codex usage metrics into the current repository. The handoff system is the core: `.agent/*` owns project facts, while `AGENTS.md`, `CLAUDE.md`, and `.codex/*` are thin adapters or compatibility pointers.

The usage system has two layers:

- Hook layer: Codex hooks record per-turn token usage, elapsed time, model, Git state, and local audit metadata.
- Project-summary layer: after Stop, a background Codex maintainer updates `.agent/usage/project-summary.json` as a task-level business value summary. It may merge multiple turns into one task and may leave consultation, pure Git, or no-deliverable turns only in hook audit metadata.

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
5. Customize `.agent/context.md`, `.agent/handoff.md`, and `.agent/workflow.md` from real project facts. Do not leave the generated initialization notes as the final context. Keep durable facts in `.agent/*`; keep `AGENTS.md`, `CLAUDE.md`, and `.codex/context.md` / `.codex/handoff.md` thin.
6. Validate:

```bash
bash -n .agent/scripts/agent-start.sh
bash -n .agent/scripts/agent-finish.sh
bash -n .agent/scripts/project-summary-maintainer.sh
python3 -m py_compile .agent/scripts/agent-usage-hook.py
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --ensure-project-summary
python3 .agent/scripts/agent-usage-hook.py --print-value-report >/tmp/agent-value-report.json
git diff --check
```

## What Gets Deployed

- `.agent/context.md`, `.agent/handoff.md`, `.agent/workflow.md`
- `.agent/prompts/start.md`, `.agent/prompts/finish.md`, `maintain-project-summary.md`
- `.agent/scripts/agent-start.sh`, `agent-finish.sh`, `agent-identity.sh`, `agent-usage-hook.py`, `project-summary-maintainer.sh`
- `.agent/usage/README.md`, local hook audit summary, and initialized `project-summary.json`
- `.codex/hooks.json`, `.codex/config.toml` hook enablement, `.codex/prompts/*`, `.codex/scripts/*`
- `.codex/context.md` and `.codex/handoff.md` compatibility pointers to `.agent/*`
- Thin `AGENTS.md` and `CLAUDE.md` entry adapters
- `.githooks/pre-commit` identity check
- `.gitignore` entries for local `.agent`/`.codex` runtime files
- A README handoff entry when `README.md` already exists

The deploy script does not commit anything.

## Handoff Source-Of-Truth Rules

- `.agent/context.md`: long-lived project goals, state, stack, key dirs, current task, latest validation, next plan, and project-specific cautions.
- `.agent/handoff.md`: the latest session goal, completed work, changed files, test results, open issues, and next-agent advice.
- `.agent/workflow.md`: common rules, commands, safety boundaries, test expectations, Git identity, and commit conventions.
- `AGENTS.md` and `CLAUDE.md`: adapter files only. They should tell the tool what to read and tool-specific requirements; do not duplicate project facts there.
- `.codex/context.md` and `.codex/handoff.md`: Codex compatibility pointers only when this skill deploys them.

## Usage Metrics Policy

- `codex-turns.jsonl` and `summary.json` are hook-layer local audit files. They are ignored by default and may contain prompts, assistant outputs, local paths, and session details.
- `project-summary.json` is commit-safe and task-level. It should contain stable metadata only: task title/summary, included turn indexes, aggregate token usage, elapsed time, AI-assessed complexity, and Git closure hints.
- Do not put raw user prompts, assistant outputs, session IDs, transcript paths, local paths, costs, or ROI in `project-summary.json`.
- Do not force every turn into `project-summary.json`; no-deliverable turns may stay only in hook audit data.
- Cost, traditional cost, savings, and ROI are derived reports. Generate them from the current summary and current unit policy:

```bash
python3 .agent/scripts/agent-usage-hook.py --write-value-report
```

- `value-report.json` is ignored by default because prices, exchange rates, and labor assumptions can change.

## Agent Metadata Hint

Before finalizing a task in a deployed repo, it is useful to write AI-authored hook-layer metadata:

```bash
python3 .agent/scripts/agent-usage-hook.py --set-current-turn-metadata \
  --description "本轮任务摘要" \
  --complexity low|medium|high \
  --reason "AI 评估复杂度的依据"
```

Complexity must be AI-assessed. Do not infer complexity from token count, elapsed time, or changed-file count. If metadata is missing, the hook records complexity as `unknown`; the background project-summary maintainer may still classify or exclude the turn at task level.

## Notes

- The deploy script is intentionally conservative: existing files are skipped unless `--force` is passed.
- If a repository already has project-specific rules, integrate them into `.agent/workflow.md` after deployment.
- If the user asks to apply this system to another repo, run the script there, then immediately review generated `.agent/*` for project-specific corrections.
