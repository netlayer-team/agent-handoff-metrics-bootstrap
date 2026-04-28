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


def context_md(project: str) -> str:
    return f"""# {project} AI Agent Project Context

## 项目目标

请在首次部署后补充本工程的目标、技术栈、运行方式和关键边界。

## 当前状态

- 已部署通用 AI 接管体系：`.agent/context.md`、`.agent/handoff.md`、`.agent/workflow.md`。
- 已部署 Codex hooks 使用量记录：`.agent/scripts/agent-usage-hook.py`。
- `project-summary.json` 只保存稳定元数据；成本和 ROI 通过派生报告生成。

## 当前任务

请在每轮收尾时更新本节：

1. 本轮完成了什么。
2. 修改了哪些文件。
3. 运行了哪些测试或验证。
4. 还有什么风险和下一步。

## 最近验证

- 首次部署后请运行 `python3 .agent/scripts/agent-usage-hook.py --rebuild-summary`。
"""


def handoff_md(project: str) -> str:
    return f"""# {project} AI Agent Handoff

## 本次会话目标

- 首次部署 AI 接管和使用量度量体系。

## 已完成

- 待当前 agent 收尾时补充。

## 改动文件

- 待当前 agent 收尾时补充。

## 测试结果

- 待当前 agent 收尾时补充。

## 遗留问题

- 待当前 agent 收尾时补充。

## 下一次接手建议

1. 先读 `.agent/context.md`、本文件、`.agent/workflow.md` 和 `README.md`。
2. 收尾前运行 `--set-current-turn-metadata` 写入 AI 任务摘要和复杂度。
3. 收尾后更新 `.agent/context.md` 和 `.agent/handoff.md`。
"""


def workflow_md(project: str) -> str:
    return f"""# {project} AI Agent Workflow

本文件是本工程的通用 AI coding agent 工作规则。`AGENTS.md`、`CLAUDE.md` 等入口只做薄适配，长期事实源应维护在 `.agent/*`。

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

## AI 使用量与业务价值记录

- Codex hooks 在 `.codex/hooks.json` 中接入；项目级 `.codex/config.toml` 需要保留 `[features].codex_hooks = true`。
- `.agent/usage/codex-turns.jsonl` 和 `.agent/usage/summary.json` 是本机运行数据，默认忽略，不提交。
- `.agent/usage/project-summary.json` 是可提交摘要，只保存 token、耗时、模型、AI 任务摘要、AI 复杂度和 Git 闭环等稳定元数据。
- `project-summary.json` 不提交完整用户 prompt、assistant 输出、session id、transcript 路径、本机路径、成本或 ROI。
- 成本、传统成本、节约额和 ROI 只能通过脚本基于 `project-summary.json` 另行生成：

```bash
python3 .agent/scripts/agent-usage-hook.py --write-value-report
```

## Git 身份

- Codex 使用 `Codex <noreply@openai.com>`。
- Claude Code 使用 `Claude <noreply@anthropic.com>`。
- 运行 `./.agent/scripts/agent-start.sh codex` 或 `./.agent/scripts/agent-start.sh claude` 会设置本仓库本地身份。
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

在用户确认前，不要修改代码，除非用户明确要求直接实现。
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
                            "statusMessage": "Recording AI usage summary",
                        }
                    ]
                }
            ],
        }
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def usage_readme() -> str:
    return """# AI Agent Usage Data

本目录用于记录 AI coding agent 的工程级使用量和业务价值指标。

- `project-summary.json`：脱敏汇总，可提交到 Git，只保存稳定元数据。
- `codex-turns.jsonl`：逐轮原始明细，包含用户输入和 assistant 输出，默认忽略，不提交。
- `summary.json`：本机完整汇总，包含本机路径等运行信息，默认忽略。
- `value-report.json`：从 `project-summary.json` 和当前脚本策略生成的派生成本报告，默认忽略，不提交。
- `pending/`、`.lock`、`hook-errors.log`：hook 运行时文件，默认忽略。

常用命令：

```bash
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --write-value-report
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
    copy_file(HOOK_ASSET, root / ".agent" / "scripts" / "agent-usage-hook.py", force=force, executable=True, report=report)
    write_file(root / ".agent" / "usage" / "README.md", usage_readme(), force=force, report=report)

    write_file(root / ".codex" / "hooks.json", hooks_json(), force=force, report=report)
    update_codex_config(root, report)
    write_file(root / ".codex" / "prompts" / "start.md", start_prompt(), force=force, report=report)
    write_file(root / ".codex" / "prompts" / "finish.md", finish_prompt(), force=force, report=report)
    write_file(root / ".codex" / "scripts" / "codex-start.sh", codex_start_sh(), force=force, executable=True, report=report)
    write_file(root / ".codex" / "scripts" / "codex-finish.sh", codex_finish_sh(), force=force, executable=True, report=report)

    write_file(root / "AGENTS.md", agents_md("codex"), force=force, report=report)
    write_file(root / "CLAUDE.md", agents_md("claude"), force=force, report=report)
    write_file(root / ".githooks" / "pre-commit", pre_commit_sh(), force=force, executable=True, report=report)
    if args.strict_commit_msg:
        write_file(root / ".githooks" / "commit-msg", commit_msg_sh(), force=force, executable=True, report=report)

    append_gitignore(root, report)

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
