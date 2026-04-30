#!/usr/bin/env python3
"""Deploy reusable AI handoff and usage metrics scaffolding into a repository."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
HOOK_ASSET = SKILL_ROOT / "assets" / "agent-usage-hook.py"
REPORT_SITE_ASSET = SKILL_ROOT / "assets" / "generate_value_report_site.py"


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_text(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def repo_root(path: Path) -> Path:
    path = path.resolve()
    root = run_text(["git", "rev-parse", "--show-toplevel"], path)
    return Path(root).resolve() if root else path


def project_key(name: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip().lower()).strip("-")
    return key or "project"


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak-{utc_stamp()}")


def write_file(
    path: Path,
    content: str,
    *,
    force: bool,
    executable: bool = False,
    report: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8", errors="ignore") == content:
        report.append(f"unchanged {path}")
        return
    if path.exists() and not force:
        report.append(f"skipped existing {path}")
        return
    if path.exists() and force:
        backup = backup_path(path)
        shutil.copy2(path, backup)
        report.append(f"backup {path} -> {backup}")
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    report.append(f"wrote {path}")


def copy_file(src: Path, dst: Path, *, force: bool, executable: bool = False, report: list[str]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and src.read_bytes() == dst.read_bytes():
        report.append(f"unchanged {dst}")
        return
    if dst.exists() and not force:
        report.append(f"skipped existing {dst}")
        return
    if dst.exists() and force:
        backup = backup_path(dst)
        shutil.copy2(dst, backup)
        report.append(f"backup {dst} -> {backup}")
    shutil.copy2(src, dst)
    if executable:
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    report.append(f"copied {dst}")


def append_gitignore(root: Path, report: list[str]) -> None:
    path = root / ".gitignore"
    block = """

# AI agent local runtime files
.agent/logs/
.agent/sessions/
.agent/tmp/
.agent/usage/*
!.agent/usage/
!.agent/usage/README.md
!.agent/usage/project-summary.json
.agent/*.jsonl
.agent/auth.json

# Codex local runtime files
.codex/logs/
.codex/sessions/
.codex/tmp/
.codex/*.jsonl
.codex/auth.json
""".lstrip()
    marker = "# AI agent local runtime files"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if marker in current:
        report.append(f"unchanged {path} usage ignore block")
        return
    path.write_text(current.rstrip() + "\n\n" + block, encoding="utf-8")
    report.append(f"updated {path}")


def append_readme_entry(root: Path, report: list[str]) -> None:
    path = root / "README.md"
    if not path.exists():
        report.append(f"skipped missing {path}")
        return

    text = path.read_text(encoding="utf-8", errors="ignore")
    marker = "## AI Agent 工程交接"
    if marker in text:
        report.append(f"unchanged {path} handoff entry")
        return

    block = """

## AI Agent 工程交接

本项目使用 `.agent/` 作为 AI coding agent 工程级接管上下文的单事实源。新会话开始前请先阅读：

1. `.agent/context.md`
2. `.agent/handoff.md`
3. `.agent/workflow.md`

可运行 `./.agent/scripts/agent-start.sh` 获取启动提示，收尾时运行 `./.agent/scripts/agent-finish.sh` 获取交接清单。
"""
    path.write_text(text.rstrip() + block + "\n", encoding="utf-8")
    report.append(f"updated {path} handoff entry")


def update_codex_config(root: Path, report: list[str]) -> None:
    path = root / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            '# Project-level Codex config. Keep runtime logs local.\n'
            'log_dir = "./logs"\n\n'
            "[features]\n"
            "codex_hooks = true\n",
            encoding="utf-8",
        )
        report.append(f"wrote {path}")
        return

    text = path.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*codex_hooks\s*=\s*true\s*$", text):
        report.append(f"unchanged {path} codex_hooks")
        return
    if re.search(r"(?m)^\s*\[features\]\s*$", text):
        text = re.sub(r"(?m)^(\s*\[features\]\s*)$", r"\1\ncodex_hooks = true", text, count=1)
    else:
        text = text.rstrip() + "\n\n[features]\ncodex_hooks = true\n"
    path.write_text(text, encoding="utf-8")
    report.append(f"updated {path}")


def codex_pointer_md(target: str) -> str:
    return f"""# Codex Compatibility Pointer

本文件只作为 Codex 兼容入口。项目 AI 接管事实源维护在 `{target}`。

请不要在本文件复制完整项目上下文，避免 `.agent/*` 与 `.codex/*` 漂移。
"""


def context_md(project: str) -> str:
    return f"""# {project} AI Agent Project Context

## 项目目标

- 本文件由部署脚本初始化。首次接管本项目的 AI agent 必须阅读 README、目录结构、构建配置、测试脚本和主要源码后，把本节改写为真实项目目标。
- 不要把本初始化说明长期保留为项目事实。

## 当前状态

- 已部署多 AI coding agent 工程级接管上下文系统。
- `.agent/context.md`、`.agent/handoff.md`、`.agent/workflow.md` 是 Codex、Claude Code 和其他 AI agent 共用的单事实源。
- `AGENTS.md` 和 `CLAUDE.md` 只作为薄入口适配器，不维护完整项目事实。
- 已部署 Codex hooks 使用量记录：`.agent/scripts/agent-usage-hook.py`。
- 已部署后台 Codex 任务价值摘要维护器：`.agent/scripts/agent-usage-hook.py --run-project-summary-agent`。
- 已部署本机 HTML 价值报告生成器：`.agent/scripts/generate-value-report-site.sh`。
- hook 层只维护本机逐轮审计数据；`project-summary.json` 由后台 AI 维护任务级业务价值摘要。
- 成本、传统成本、节约额和 ROI 通过派生报告生成，不写入 `project-summary.json`。

## 技术栈

- 首次接管时根据真实项目补充语言、框架、数据库/存储、测试、部署方式和外部依赖。

## 关键目录

- `.agent/`：AI agent 共用项目上下文、交接、工作规则、提示和脚本。
- `.agent/usage/`：AI 使用量数据；`project-summary.json` 是可提交任务级摘要，其他运行数据默认忽略。
- `.codex/`：Codex 专属配置、hooks 和兼容入口。
- `.githooks/`：项目级 Git hooks。
- 首次接管时继续补充本项目业务源码、测试、文档和部署目录。

## 当前任务

- 当前没有未完成任务记录。下一次接手时先阅读 `.agent/handoff.md`，再根据用户请求和项目现状更新本节。

## 最近变更

- 首次部署了 AI agent 工程级接管上下文系统、入口适配器、启动/收尾脚本、Git 身份检查和 Codex 使用量记录。

## 最近验证

- 首次部署后请运行 `bash -n .agent/scripts/agent-start.sh`。
- 首次部署后请运行 `bash -n .agent/scripts/agent-finish.sh`。
- 首次部署后请运行 `bash -n .agent/scripts/project-summary-maintainer.sh`。
- 首次部署后请运行 `bash -n .agent/scripts/generate-value-report-site.sh`。
- 首次部署后请运行 `python3 .agent/scripts/agent-usage-hook.py --rebuild-summary`。

## 下一步计划

1. 当前 agent 根据真实项目补全本文件的项目目标、技术栈、关键目录、常用命令和边界。
2. 把本轮部署结果写入 `.agent/handoff.md`。
3. 根据项目实际情况补充 `.agent/workflow.md` 中的测试、构建和提交规则。

## 注意事项

- 项目长期事实、当前状态、测试结果和下一步计划只维护在 `.agent/*`。
- `AGENTS.md`、`CLAUDE.md` 和 `.codex/context.md` / `.codex/handoff.md` 只做入口或兼容指针。
- 不提交 `.agent/logs/`、`.agent/sessions/`、`.agent/tmp/`、本地认证文件或 Codex 本地运行数据。
- 修改后优先运行项目测试、构建或静态检查；无法运行时在 `.agent/handoff.md` 写清原因和剩余风险。
"""


def handoff_md(project: str) -> str:
    return f"""# {project} AI Agent Handoff

## 本次会话目标

- 首次部署 AI agent 工程级接管上下文系统和使用量度量体系。

## 已完成

- 已创建 `.agent/` 单事实源结构。
- 已创建 `AGENTS.md` 和 `CLAUDE.md` 薄入口适配器。
- 已创建启动/收尾提示和脚本。
- 已接入 Codex 使用量记录和项目级 Git 身份检查。
- 已接入 Stop 后后台 Codex 任务级价值摘要维护器。
- 已接入提交后 Git 闭环回填和本机 HTML 价值报告刷新。

## 改动文件

- `AGENTS.md`
- `CLAUDE.md`
- `.agent/context.md`
- `.agent/handoff.md`
- `.agent/workflow.md`
- `.agent/prompts/start.md`
- `.agent/prompts/finish.md`
- `.agent/scripts/`
- `.agent/usage/`
- `.agent/scripts/generate_value_report_site.py`
- `.codex/`
- `.githooks/pre-commit`
- `.gitignore`

## 测试结果

- 首次部署后需要运行 `bash -n .agent/scripts/agent-start.sh`。
- 首次部署后需要运行 `bash -n .agent/scripts/agent-finish.sh`。
- 首次部署后需要运行 `bash -n .agent/scripts/project-summary-maintainer.sh`。
- 首次部署后需要运行 `bash -n .agent/scripts/generate-value-report-site.sh`。
- 首次部署后需要运行 `python3 -m py_compile .agent/scripts/agent-usage-hook.py`。
- 首次部署后需要运行 `python3 .agent/scripts/agent-usage-hook.py --rebuild-summary`。

## 遗留问题

- 当前 agent 需要根据真实项目情况补全 `.agent/context.md` 和 `.agent/workflow.md`。
- 当前 agent 需要把本次部署后的实际验证结果更新到本文件。

## 下一次接手建议

1. 先读 `.agent/context.md`、本文件、`.agent/workflow.md` 和 `README.md`。
2. 确认 `AGENTS.md`、`CLAUDE.md`、`.codex/context.md` 和 `.codex/handoff.md` 没有复制项目事实，只指向 `.agent/*`。
3. 检查 `.agent/usage/project-summary.json` 是否由后台 maintainer 维护为任务级摘要，而不是逐轮流水。
4. 收尾前运行 `--set-current-turn-metadata` 写入 AI 任务摘要和复杂度。
5. 收尾时更新 `.agent/context.md` 和 `.agent/handoff.md`。
"""


def workflow_md(project: str) -> str:
    return f"""# {project} AI Agent Workflow

本文件是本工程的通用 AI coding agent 工作规则。`AGENTS.md`、`CLAUDE.md` 等入口只做薄适配，长期事实源应维护在 `.agent/*`。

## 项目定位

- 本文件描述 AI agent 如何接管、开发、验证和收尾本项目。
- 项目业务目标、当前状态和下一步计划维护在 `.agent/context.md`。
- 最近一次会话交接维护在 `.agent/handoff.md`。

## 启动要求

每次开始工作前先阅读：

1. `.agent/context.md`
2. `.agent/handoff.md`
3. `.agent/workflow.md`
4. `README.md`（如存在）
5. 当前任务相关源码和文档

可运行：

```bash
./.agent/scripts/agent-start.sh codex
```

也可以显式切换身份：

```bash
./.agent/scripts/agent-start.sh claude
./.agent/scripts/agent-start.sh none
```

## 收尾要求

每次完成阶段性任务后，必须更新：

1. `.agent/context.md`
2. `.agent/handoff.md`

更新内容包括完成事项、修改文件、测试结果、风险和下一步。

收尾前写入本轮 AI 任务元数据：

```bash
python3 .agent/scripts/agent-usage-hook.py --set-current-turn-metadata \\
  --description "本轮任务摘要" \\
  --complexity low|medium|high \\
  --reason "AI 评估复杂度的依据"
```

可运行：

```bash
./.agent/scripts/agent-finish.sh
```

## 常用命令

首次部署后至少验证：

```bash
bash -n .agent/scripts/agent-start.sh
bash -n .agent/scripts/agent-finish.sh
bash -n .agent/scripts/project-summary-maintainer.sh
bash -n .agent/scripts/generate-value-report-site.sh
python3 -m py_compile .agent/scripts/agent-usage-hook.py
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
git diff --check
```

请根据项目实际情况补充构建、测试、格式化、lint、启动和部署命令。

## 开发约定

- 改代码前先阅读相关文件，不凭空假设项目结构。
- 优先沿用项目已有框架、目录、命名和测试风格。
- 不做无关重构，不引入和当前任务无关的格式化 churn。
- 如果用户只要求分析，先给出基于文件和运行结果的诊断；如果用户要求修复，直接实现并验证。

## 安全边界

- 不提交 API key、token、认证文件、私有会话、运行日志或本地路径明细。
- `.agent/auth.json`、`.agent/logs/`、`.agent/sessions/`、`.agent/tmp/` 和 `.codex/*` 本地运行数据必须保持忽略。
- `project-summary.json` 只保留脱敏稳定元数据；成本和 ROI 报告本地派生。

## 测试建议

- 小改动至少运行相关单测、类型检查、lint 或脚本语法检查。
- 影响用户流程、CLI、Web UI 或跨模块契约时，补充端到端或集成验证。
- 无法运行测试时，在 `.agent/handoff.md` 记录具体命令、失败原因和剩余风险。

## AI 使用量与业务价值记录

- Codex hooks 在 `.codex/hooks.json` 中接入；项目级 `.codex/config.toml` 需要保留 `[features].codex_hooks = true`。
- hook 层负责本机逐轮审计：token、耗时、模型、Git 状态、prompt/assistant 本地明细等。
- `.agent/usage/codex-turns.jsonl` 和 `.agent/usage/summary.json` 是本机运行数据，默认忽略，不提交。
- Stop hook 会在记录本轮审计数据后，后台启动 `python3 .agent/scripts/agent-usage-hook.py --run-project-summary-agent --record-id <record-id>`。
- project-summary 层由后台 Codex maintainer 维护：允许把多轮对话合并为一个任务，也允许咨询、纯 Git 提交、无交付轮次只留在 hook 元数据中。
- `.agent/usage/project-summary.json` 是可提交任务级业务价值摘要，只保存稳定、脱敏、可审查的任务级字段。
- `project-summary.json` 不提交完整用户 prompt、assistant 输出、session id、transcript 路径、本机路径、成本或 ROI。
- `.githooks/post-commit` 会尝试把最新未闭环任务绑定到刚生成的提交 SHA，并刷新默认忽略的 HTML 价值报告。
- 成本、传统成本、节约额和 ROI 只能通过脚本基于 `project-summary.json` 另行生成：

```bash
python3 .agent/scripts/agent-usage-hook.py --write-value-report
./.agent/scripts/generate-value-report-site.sh
```

## Git 身份

- Codex 使用 `Codex <noreply@openai.com>`。
- Claude Code 使用 `Claude <noreply@anthropic.com>`。
- 运行 `./.agent/scripts/agent-start.sh codex` 或 `./.agent/scripts/agent-start.sh claude` 会设置本仓库本地身份。
- `.githooks/pre-commit` 会在提交前检查 AI agent 身份，避免 AI 提交继承人工 Git 身份。

## Git 提交规范

- 提交前查看 `git status --short` 和相关 diff，确认只提交本任务范围内的改动。
- 提交信息优先使用 Conventional Commits，例如 `docs(agent): 增加 AI 接管上下文`。
- 如果启用了 `--strict-commit-msg`，`.githooks/commit-msg` 会执行基础 Conventional Commits 检查。
"""


def start_prompt() -> str:
    return """请接管本工程。

请先阅读：

- `.agent/context.md`
- `.agent/handoff.md`
- `.agent/workflow.md`
- `README.md`
- 当前任务相关源码和文档

如果你是 Codex，请确保本仓库 Git 身份为 `Codex <noreply@openai.com>`。
如果你是 Claude Code，请确保本仓库 Git 身份为 `Claude <noreply@anthropic.com>`。

然后输出：

1. 你对项目当前状态的理解
2. 当前任务或你认为最合理的下一步
3. 下一步执行计划

如果用户已经明确要求实现或修复，请在完成上述阅读后直接继续执行，并在收尾时更新 `.agent/context.md` 和 `.agent/handoff.md`。
"""


def finish_prompt() -> str:
    return """请收尾本次 AI coding agent 会话。

请完成：

1. 总结本次完成内容
2. 更新 `.agent/context.md`
3. 更新 `.agent/handoff.md`
4. 写入本轮 AI 任务摘要和复杂度：

```bash
python3 .agent/scripts/agent-usage-hook.py --set-current-turn-metadata \
  --description "本轮任务摘要" \
  --complexity low|medium|high \
  --reason "AI 评估复杂度的依据"
```

5. 列出修改文件
6. 列出测试/构建结果
7. 写清楚下一次 AI agent 应该从哪里继续

如果本次没有改代码，也要说明只改了文档、脚本或上下文。
"""


def maintain_project_summary_prompt() -> str:
    return """你是本项目的后台 Codex 任务价值摘要维护器。

目标：根据 hook 层本机审计数据，维护 `.agent/usage/project-summary.json` 这个可提交的任务级业务价值摘要。

请严格遵守：

1. 只允许修改 `.agent/usage/project-summary.json`。
2. 读取 `.agent/usage/codex-turns.jsonl`、`.agent/usage/summary.json` 和现有 `.agent/usage/project-summary.json`。
3. hook 层数据是逐轮审计事实源，保留在本机；不要把原始 prompt、assistant 输出、session_id、turn_id、transcript_path、本机绝对路径、API key、token 或 secret 写入 `project-summary.json`。
4. `project-summary.json` 是任务级业务价值摘要，不是逐轮流水。可以把多个 turn 合并成一个任务。
5. 咨询、纯 Git 提交、hook smoke、上下文整理、无交付结果等轮次，如果没有独立业务交付，可以只留在 hook 审计数据中，不要加入 `tasks[]`。
6. 不要写成本、传统成本、节约额或 ROI；这些由脚本从 `project-summary.json` 派生。
7. 保持 JSON 有效、稳定、可 diff。不要输出 Markdown，不要修改其他文件。

推荐 schema：

```json
{
  "schema_version": 3,
  "project": "<repo-name>",
  "updated_at": "<UTC ISO timestamp>",
  "maintained_by": "codex_project_summary_maintainer",
  "source_layers": {
    "hook_audit_records": ".agent/usage/codex-turns.jsonl",
    "local_audit_summary": ".agent/usage/summary.json",
    "task_value_summary": ".agent/usage/project-summary.json"
  },
  "totals": {
    "audit_recorded_turns": 0,
    "value_task_count": 0,
    "included_turn_count": 0,
    "excluded_turn_count": 0
  },
  "tasks": [
    {
      "task_id": "task-YYYYMMDD-001",
      "title": "短标题",
      "summary": "任务级交付摘要",
      "status": "delivered",
      "value_included": true,
      "included_turn_indexes": [1],
      "started_at": "<UTC ISO timestamp>",
      "ended_at": "<UTC ISO timestamp>",
      "elapsed_seconds": 0,
      "primary_model": "unknown",
      "models": ["unknown"],
      "token_usage": {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0
      },
      "git": {
        "closed_loop": false,
        "commits": []
      },
      "business_value": {
        "description": "面向业务的价值说明",
        "complexity": "low",
        "complexity_reason": "复杂度判断依据",
        "complexity_source": "project_summary_maintainer"
      }
    }
  ],
  "privacy": {
    "contains_user_prompts": false,
    "contains_assistant_outputs": false,
    "contains_session_ids": false,
    "contains_transcript_paths": false,
    "contains_local_paths": false,
    "contains_derived_costs": false,
    "contains_roi": false,
    "task_descriptions_are_ai_maintained": true
  },
  "notes": []
}
```

维护规则：

- 优先复用已有 `task_id`，不要因为新增一轮就重排旧任务。
- `included_turn_indexes` 使用 hook 审计记录的顺序号，不使用 session_id 或 turn_id。
- `token_usage` 和 `elapsed_seconds` 是该任务包含 turns 的合计。
- `primary_model` 选择该任务主要使用的模型；`models` 列出参与模型。
- `business_value.complexity` 只能是 `low`、`medium`、`high` 或 `unknown`。
- 如果无法可靠判断业务价值，保守地把该轮排除在 `tasks[]` 外，只更新 totals。
"""


def project_summary_maintainer_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

USAGE_DIR="${AGENT_USAGE_DIR:-$ROOT/.agent/usage}"

export AGENT_USAGE_DIR="$USAGE_DIR"
python3 "$ROOT/.agent/scripts/agent-usage-hook.py" --run-project-summary-agent "$@"
"""


def generate_value_report_site_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
USAGE_DIR="${USAGE_DIR:-$ROOT_DIR/.agent/usage}"
OUTPUT="${OUTPUT:-$ROOT_DIR/.agent/usage/value-report.html}"

python3 "$ROOT_DIR/.agent/scripts/generate_value_report_site.py" \\
  --usage-dir "$USAGE_DIR" \\
  --output "$OUTPUT" \\
  "$@"
"""


def agent_start_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

if [ "${1:-}" != "" ]; then
  ./.agent/scripts/agent-identity.sh "$1"
else
  ./.agent/scripts/agent-identity.sh --auto
fi

echo "== AI Agent Project Start =="
echo "Project: $ROOT"
echo
echo "请把下面这段作为 AI coding agent 启动提示："
echo "----------------------------------------"
cat .agent/prompts/start.md
echo "----------------------------------------"
"""


def agent_finish_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

echo "== AI Agent Project Finish =="
echo "Project: $ROOT"
echo
echo "请把下面这段发给 AI coding agent："
echo "----------------------------------------"
cat .agent/prompts/finish.md
echo "----------------------------------------"
"""


def agent_identity_sh(config_key: str) -> str:
    return f"""#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

mode="set"
auto=0

case "${{1:-}}" in
  --check)
    mode="check"
    shift
    ;;
  --auto)
    auto=1
    shift
    ;;
esac

lower() {{
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}}

normalize_agent() {{
  case "$(lower "$1")" in
    codex|openai-codex)
      printf 'codex'
      ;;
    claude|claude-code|anthropic-claude)
      printf 'claude'
      ;;
    human|manual|none)
      printf 'none'
      ;;
    *)
      return 1
      ;;
  esac
}}

detect_agent() {{
  local configured

  if [ "${{1:-}}" != "" ]; then
    normalize_agent "$1"
    return
  fi

  if [ "${{AI_AGENT:-}}" != "" ]; then
    normalize_agent "$AI_AGENT"
    return
  fi

  if [ "${{CODEX_CI:-}}${{CODEX_THREAD_ID:-}}${{CODEX_MANAGED_BY_NPM:-}}${{CODEX_HOME:-}}" != "" ]; then
    printf 'codex'
    return
  fi

  if [ "${{CLAUDECODE:-}}${{CLAUDE_CODE:-}}${{CLAUDE_CODE_SSE_PORT:-}}${{CLAUDE_PROJECT_DIR:-}}" != "" ]; then
    printf 'claude'
    return
  fi

  if [ "$mode" = "check" ]; then
    configured="$(git config --get {config_key}.agent 2>/dev/null || true)"
    if [ "$configured" != "" ]; then
      normalize_agent "$configured"
      return
    fi
  fi

  return 1
}}

agent="$(detect_agent "${{1:-}}" || true)"

if [ "$agent" = "" ]; then
  if [ "$auto" -eq 1 ] || [ "$mode" = "check" ]; then
    exit 0
  fi
  echo "Usage: ./.agent/scripts/agent-identity.sh codex|claude|none" >&2
  exit 2
fi

if [ "$agent" = "none" ]; then
  git config --unset {config_key}.agent 2>/dev/null || true
  echo "Cleared {config_key}.agent marker. Git user.name/user.email were not changed."
  exit 0
fi

case "$agent" in
  codex)
    expected_name="Codex"
    expected_email="noreply@openai.com"
    ;;
  claude)
    expected_name="Claude"
    expected_email="noreply@anthropic.com"
    ;;
esac

ensure_project_git_settings() {{
  git config core.hooksPath .githooks
  if [ -f .gitmessage ]; then
    git config commit.template .gitmessage
  fi
}}

if [ "$mode" = "check" ]; then
  actual_name="$(git config --get user.name 2>/dev/null || true)"
  actual_email="$(git config --get user.email 2>/dev/null || true)"

  if [ "$actual_name" != "$expected_name" ] || [ "$actual_email" != "$expected_email" ]; then
    git config user.name "$expected_name"
    git config user.email "$expected_email"
    git config {config_key}.agent "$agent"
    ensure_project_git_settings
    echo "Git identity corrected for $agent. Please run git commit again." >&2
    exit 1
  fi
  exit 0
fi

git config user.name "$expected_name"
git config user.email "$expected_email"
git config {config_key}.agent "$agent"
ensure_project_git_settings

echo "Git identity set for $agent: $expected_name <$expected_email>"
"""


def codex_start_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

./.agent/scripts/agent-identity.sh codex

echo "== Codex Project Start =="
echo "Project: $ROOT"
echo
echo "请把下面这段作为 Codex 启动提示："
echo "----------------------------------------"
cat .codex/prompts/start.md
echo "----------------------------------------"
"""


def codex_finish_sh() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

echo "== Codex Project Finish =="
echo "Project: $ROOT"
echo
echo "请把下面这段发给 Codex："
echo "----------------------------------------"
cat .codex/prompts/finish.md
echo "----------------------------------------"
"""


def hooks_json() -> str:
    data = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python3 "$(git rev-parse --show-toplevel)/.agent/scripts/agent-usage-hook.py"',
                            "timeout": 10,
                            "statusMessage": "Recording AI usage start",
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python3 "$(git rev-parse --show-toplevel)/.agent/scripts/agent-usage-hook.py"',
                            "timeout": 30,
                            "statusMessage": "Recording AI usage and queueing value summary",
                        }
                    ]
                }
            ],
        }
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def usage_readme() -> str:
    return """# AI Agent Usage Data

本目录用于记录 AI coding agent 的工程级使用量和业务价值指标。数据分两层：

- hook 层：逐轮记录 token、耗时、模型、Git 状态等本机审计数据。
- project-summary 层：由后台 Codex maintainer 维护任务级业务价值摘要。

- `project-summary.json`：任务级脱敏摘要，可提交到 Git；允许多轮合并为一个任务。
- `codex-turns.jsonl`：逐轮原始明细，包含用户输入和 assistant 输出，默认忽略，不提交。
- `summary.json`：hook 层本机审计汇总，包含本机路径等运行信息，默认忽略。
- `value-report.json`：从 `project-summary.json` 和当前脚本策略生成的派生成本报告，默认忽略，不提交。
- `value-report.html`：本机交互式 HTML 价值报告，默认忽略，不提交。
- `project-summary-agent/`：后台 maintainer 的 prompt、schema、输出和 job 日志，默认忽略。
- `pending/`、`.lock`、`hook-errors.log`：hook 运行时文件，默认忽略。

常用命令：

```bash
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --refresh-project-summary
python3 .agent/scripts/agent-usage-hook.py --write-value-report
./.agent/scripts/generate-value-report-site.sh
```
"""


def agents_md(tool: str) -> str:
    label = "Codex" if tool == "codex" else "Claude Code"
    command = "codex" if tool == "codex" else "claude"
    return f"""# {'AGENTS.md' if tool == 'codex' else 'CLAUDE.md'}

本文件是 {label} 的项目入口适配器。项目事实源不在本文件中维护，避免多个 AI coding agent 入口重复漂移。

## 必读文件

每次开始工作前，请先阅读：

1. `.agent/context.md`
2. `.agent/handoff.md`
3. `.agent/workflow.md`
4. `README.md`
5. 当前任务相关源码和文档

## 工作要求

- 遵守 `.agent/workflow.md` 中的项目规则、测试要求和 Git 提交规范。
- 收尾前必须更新 `.agent/context.md` 和 `.agent/handoff.md`。
- 收尾前写入本轮 AI 任务摘要和复杂度。
- 如果测试无法运行，需要写清楚原因和剩余风险。

## 启动提示

可运行：

```bash
./.agent/scripts/agent-start.sh {command}
```

## 收尾提示

可运行：

```bash
./.agent/scripts/agent-finish.sh
```
"""


def pre_commit_sh() -> str:
    return """#!/usr/bin/env sh
set -eu

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

if [ -x ./.agent/scripts/agent-identity.sh ]; then
  ./.agent/scripts/agent-identity.sh --check
fi
"""


def post_commit_sh() -> str:
    return """#!/usr/bin/env sh
set -eu

if [ "${AGENT_USAGE_POST_COMMIT_ACTIVE:-}" = "1" ]; then
  exit 0
fi

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

if [ -x ./.agent/scripts/agent-usage-hook.py ]; then
  AGENT_USAGE_POST_COMMIT_ACTIVE=1 \\
    python3 ./.agent/scripts/agent-usage-hook.py --finalize-latest-project-task-from-git >/dev/null 2>&1 || true
fi

if [ -x ./.agent/scripts/generate-value-report-site.sh ]; then
  AGENT_USAGE_POST_COMMIT_ACTIVE=1 \\
    ./.agent/scripts/generate-value-report-site.sh >/dev/null 2>&1 || true
fi
"""


def commit_msg_sh() -> str:
    return """#!/usr/bin/env sh
set -eu

message_file="${1:-}"
if [ -z "$message_file" ] || [ ! -f "$message_file" ]; then
  echo "commit-msg: missing commit message file" >&2
  exit 1
fi

subject="$(sed -n '1p' "$message_file")"
case "$subject" in
  Merge\\ *|Revert\\ *|fixup!\\ *|squash!\\ *)
    exit 0
    ;;
esac

pattern='^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(\\([A-Za-z0-9._/-]+\\))?(!)?: .+'
if ! printf '%s\\n' "$subject" | grep -Eq "$pattern"; then
  echo "Invalid commit message. Use Conventional Commits." >&2
  exit 1
fi
"""


def deploy(args: argparse.Namespace) -> int:
    root = repo_root(Path(args.repo or os.getcwd()))
    project = args.project_name or root.name
    key = project_key(project)
    report: list[str] = []
    force = bool(args.force)

    write_file(root / ".agent" / "context.md", context_md(project), force=force, report=report)
    write_file(root / ".agent" / "handoff.md", handoff_md(project), force=force, report=report)
    write_file(root / ".agent" / "workflow.md", workflow_md(project), force=force, report=report)
    write_file(root / ".agent" / "prompts" / "start.md", start_prompt(), force=force, report=report)
    write_file(root / ".agent" / "prompts" / "finish.md", finish_prompt(), force=force, report=report)
    write_file(root / ".agent" / "scripts" / "agent-start.sh", agent_start_sh(), force=force, executable=True, report=report)
    write_file(root / ".agent" / "scripts" / "agent-finish.sh", agent_finish_sh(), force=force, executable=True, report=report)
    write_file(root / ".agent" / "scripts" / "agent-identity.sh", agent_identity_sh(key), force=force, executable=True, report=report)
    write_file(root / ".agent" / "scripts" / "project-summary-maintainer.sh", project_summary_maintainer_sh(), force=force, executable=True, report=report)
    write_file(root / ".agent" / "scripts" / "generate-value-report-site.sh", generate_value_report_site_sh(), force=force, executable=True, report=report)
    copy_file(HOOK_ASSET, root / ".agent" / "scripts" / "agent-usage-hook.py", force=force, executable=True, report=report)
    copy_file(REPORT_SITE_ASSET, root / ".agent" / "scripts" / "generate_value_report_site.py", force=force, executable=True, report=report)
    write_file(root / ".agent" / "usage" / "README.md", usage_readme(), force=force, report=report)

    write_file(root / ".codex" / "hooks.json", hooks_json(), force=force, report=report)
    update_codex_config(root, report)
    write_file(root / ".codex" / "prompts" / "start.md", start_prompt(), force=force, report=report)
    write_file(root / ".codex" / "prompts" / "finish.md", finish_prompt(), force=force, report=report)
    write_file(root / ".codex" / "context.md", codex_pointer_md(".agent/context.md"), force=force, report=report)
    write_file(root / ".codex" / "handoff.md", codex_pointer_md(".agent/handoff.md"), force=force, report=report)
    write_file(root / ".codex" / "scripts" / "codex-start.sh", codex_start_sh(), force=force, executable=True, report=report)
    write_file(root / ".codex" / "scripts" / "codex-finish.sh", codex_finish_sh(), force=force, executable=True, report=report)

    write_file(root / "AGENTS.md", agents_md("codex"), force=force, report=report)
    write_file(root / "CLAUDE.md", agents_md("claude"), force=force, report=report)
    write_file(root / ".githooks" / "pre-commit", pre_commit_sh(), force=force, executable=True, report=report)
    write_file(root / ".githooks" / "post-commit", post_commit_sh(), force=force, executable=True, report=report)
    if args.strict_commit_msg:
        write_file(root / ".githooks" / "commit-msg", commit_msg_sh(), force=force, executable=True, report=report)

    append_gitignore(root, report)
    append_readme_entry(root, report)

    usage_dir = root / ".agent" / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(root / ".agent" / "scripts" / "agent-usage-hook.py"), "--rebuild-summary"],
        cwd=str(root),
        check=False,
    )

    if args.agent != "none":
        subprocess.run(
            [str(root / ".agent" / "scripts" / "agent-identity.sh"), args.agent],
            cwd=str(root),
            check=False,
        )

    print(json.dumps({"repo": str(root), "project": project, "changes": report}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.getcwd(), help="repository root, defaults to current directory")
    parser.add_argument("--project-name", help="display name for generated .agent docs")
    parser.add_argument("--agent", choices=["codex", "claude", "none"], default="codex", help="set local Git identity marker")
    parser.add_argument("--force", action="store_true", help="overwrite existing generated files after backing them up")
    parser.add_argument("--strict-commit-msg", action="store_true", help="install a basic Conventional Commits commit-msg hook")
    args = parser.parse_args()
    return deploy(args)


if __name__ == "__main__":
    raise SystemExit(main())
