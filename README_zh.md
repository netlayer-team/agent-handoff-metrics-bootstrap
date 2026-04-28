# Agent Handoff Metrics Bootstrap

[English README](README.md)

AI Agent 接管与度量引导器。

> 让 AI 编程代理像真正工程队友一样恢复上下文、验证工作并汇报结果。

![HandoffKit 总览图：AI 编程代理的项目记忆和交付度量](docs/diagrams/handoffkit-overview.svg)

`agent-handoff-metrics-bootstrap` 帮助团队让 AI coding-agent 的工作可恢复、可审计、可度量。

如果你遇到过换电脑、换编程代理工具、多人协作接手后，新的 AI 会话重新从零开始的问题，或者 AI 确实帮了忙但很难沉淀交付证据和价值报告，这个仓库就是为了解决这类问题。

它会把项目本地记忆、交接提示、轻量 agent 适配器、Git 身份检查、Codex hooks、使用量/价值报告部署进现有仓库。

部署后，Codex、Claude Code、Cursor、Gemini CLI 或其他 coding agents 能够：

- 跨机器、跨工具、跨会话继续开发；
- 在改代码前读取长期项目记忆；
- 在每个任务结束后写清楚交接说明；
- 把每轮 AI 辅助工作记录为安全的项目级使用量指标；
- 把 AI 工作关联到 Git 结果、成本和 ROI 报告。

自动化 turn 采集目前通过 Codex hooks 实现。项目记忆和交接工作流本身是工具无关的，其他 coding agents 也可以复用。

核心理念很简单：

> 不同步聊天历史。同步项目记忆、交接状态、决策、验证结果和交付证据。

可传播短名：**HandoffKit for AI Coding Agents**。

## 文档

根 README 刻意保持简短。详细架构、流程和图放在 `docs/`：

- [中文设计文档](docs/design_zh.md)：架构、运行时采集流程、数据边界、度量模型和 Git 闭环。
- [Design](docs/design.md): architecture, runtime collection flow, data boundaries, metrics model, and Git closure.
- [Docs index](docs/README.md)

## 快速开始

安装为本地 Codex skill：

```bash
mkdir -p /root/.codex/skills
git clone git@github.com:netlayer-team/agent-handoff-metrics-bootstrap.git \
  /root/.codex/skills/agent-handoff-metrics-bootstrap
```

部署到目标仓库：

```bash
python3 /root/.codex/skills/agent-handoff-metrics-bootstrap/scripts/deploy_agent_system.py --repo "$PWD"
```

常用选项：

```bash
--project-name "Project Name"   # 自定义生成文档中的项目名称
--agent codex|claude|none       # 默认 codex；设置仓库本地 Git 身份
--force                         # 备份已有文件后覆盖
--strict-commit-msg             # 增加基础 Conventional Commits commit-msg hook
```

部署后验证：

```bash
git status --short
python3 -m py_compile .agent/scripts/agent-usage-hook.py
python3 .agent/scripts/agent-usage-hook.py --rebuild-summary
python3 .agent/scripts/agent-usage-hook.py --print-value-report >/tmp/agent-value-report.json
git diff --check
```

## 部署后生成内容

在目标仓库运行部署脚本后，可能生成：

- `.agent/context.md`、`.agent/handoff.md`、`.agent/workflow.md`
- `.agent/prompts/start.md`、`.agent/prompts/finish.md`
- `.agent/scripts/agent-start.sh`、`agent-finish.sh`、`agent-identity.sh`、`agent-usage-hook.py`
- `.agent/usage/README.md` 和重新生成的 `project-summary.json`
- `.codex/hooks.json`、`.codex/config.toml`、`.codex/prompts/*`、`.codex/scripts/*`
- 轻量 `AGENTS.md` 和 `CLAUDE.md` 入口适配器
- `.githooks/pre-commit`，以及可选的 `.githooks/commit-msg`
- `.gitignore` 中的 agent/Codex 本地运行文件忽略规则

部署脚本不会自动提交 Git。

## Agent 工作流

生成后的项目为每个 AI coding agent 提供统一入口：

```bash
./.agent/scripts/agent-start.sh codex
./.agent/scripts/agent-finish.sh
```

`agent-start.sh` 会设置或检查仓库本地 Git 身份，并打印项目启动提示。`agent-finish.sh` 会打印当前会话的交接和总结检查清单。

生成的身份：

- Codex：`Codex <noreply@openai.com>`
- Claude Code：`Claude <noreply@anthropic.com>`

生成的 pre-commit hook 会在提交前检查当前 agent 身份，避免 AI 生成的提交悄悄继承人工 Git 身份。

## 使用量和价值度量

复制到目标仓库的 `agent-usage-hook.py` 会把 Codex turn 使用量记录到 `.agent/usage/`。

可提交输出：

- `.agent/usage/project-summary.json`：稳定摘要元数据，包含模型、token 用量、耗时、AI 任务摘要、AI 复杂度和 Git 闭环状态。

默认忽略的本地运行输出：

- `codex-turns.jsonl`：逐轮本地原始记录。
- `summary.json`：包含本机细节的本地完整汇总。
- `value-report.json`：派生的成本、传统成本、节约额和 ROI 报告。
- pending 文件、lock 文件和 hook 错误日志。

在收尾 AI 生成任务前，写入本轮任务元数据：

```bash
python3 .agent/scripts/agent-usage-hook.py --set-current-turn-metadata \
  --description "本轮任务摘要" \
  --complexity low|medium|high \
  --reason "AI 评估复杂度的依据"
```

成本和 ROI 报告来自当前策略假设，应按需重新生成，不直接提交：

```bash
python3 .agent/scripts/agent-usage-hook.py --write-value-report
```

## 仓库结构

- `SKILL.md`：Codex skill 入口和操作说明。
- `scripts/deploy_agent_system.py`：部署脚本，把可复用的 `.agent`、`.codex`、`.githooks` 脚手架写入目标仓库。
- `assets/agent-usage-hook.py`：Codex hook 脚本，部署时复制到目标仓库。
- `agents/openai.yaml`：agent marketplace/display 元数据。
- `docs/`：详细设计文档和图源码。
- `references/`：预留给更长的参考资料、示例或设计说明。

## 开发检查

发布本 skill 的改动前，先运行轻量检查：

```bash
python3 -m py_compile assets/agent-usage-hook.py scripts/deploy_agent_system.py
python3 scripts/deploy_agent_system.py --help
```

部署 smoke test：

```bash
tmp_repo="$(mktemp -d)"
git -C "$tmp_repo" init
python3 scripts/deploy_agent_system.py --repo "$tmp_repo" --project-name Smoke --agent none
python3 -m py_compile "$tmp_repo/.agent/scripts/agent-usage-hook.py"
python3 "$tmp_repo/.agent/scripts/agent-usage-hook.py" --rebuild-summary
```

## 隐私边界

`project-summary.json` 被刻意限制为稳定、可审查的元数据。它不应包含原始 prompt、assistant 输出、session ID、transcript 路径、本地路径、成本、ROI、API key、token 或 secret。

详细本地用量文件适合本地分析，但默认会被忽略，不应提交。
