#!/usr/bin/env python3
"""Record Codex turn usage into the project-local .agent directory.

This script is designed to be called by Codex hooks. It records the prompt at
UserPromptSubmit and completes the turn record at Stop by reading Codex's
transcript token_count events.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Linux compatibility
    fcntl = None  # type: ignore[assignment]


USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

PROJECT_SUMMARY_SCHEMA_VERSION = 3
COMPLEXITY_LEVELS = {"low", "medium", "high", "unknown"}
PROJECT_TASK_STATUSES = {"in_progress", "completed", "cancelled"}
PROJECT_TASK_CATEGORIES = {
    "implementation",
    "debugging",
    "design",
    "review",
    "ops",
    "documentation",
    "maintenance",
    "other",
}

MAINTAINER_ENV_FLAG = "AGENT_USAGE_MAINTAINER"
MAINTAINER_ACTIVE_ENV = "AGENT_USAGE_MAINTAINER_ACTIVE"
MAINTAINER_MODEL_ENV = "AGENT_USAGE_MAINTAINER_MODEL"
MAINTAINER_SYNC_ENV = "AGENT_USAGE_MAINTAINER_SYNC"
DEFAULT_MAINTAINER_MODEL = "gpt-5.4-mini"

DEFAULT_USD_TO_CNY = 7.20
DEFAULT_ENGINEER_HOURLY_RATE_CNY = 300.0
DEFAULT_TRADITIONAL_HOURS = {
    "low": 0.5,
    "medium": 2.0,
    "high": 8.0,
    "unknown": 1.0,
}

# Standard OpenAI API text-token pricing, USD per 1M tokens. Keep this table
# explicit so committed summaries remain reproducible even when live prices move.
MODEL_PRICES_USD_PER_1M: dict[str, dict[str, float | None]] = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
}

MODEL_PRICE_PREFIXES = (
    ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-5.5", "gpt-5.5"),
    ("gpt-5.4", "gpt-5.4"),
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


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


def resolve_repo_root(cwd_value: str | None) -> Path:
    cwd = Path(cwd_value or os.getcwd()).resolve()
    root = run_text(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(root).resolve() if root else cwd


def usage_dir(root: Path) -> Path:
    override = os.environ.get("AGENT_USAGE_DIR")
    if override:
        path = Path(override)
        return path if path.is_absolute() else root / path
    return root / ".agent" / "usage"


def safe_name(value: str | None) -> str:
    value = value or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:180]


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    usage = {key: int(raw.get(key) or 0) for key in USAGE_KEYS if key != "uncached_input_tokens"}
    usage["uncached_input_tokens"] = max(
        0,
        usage.get("input_tokens", 0) - usage.get("cached_input_tokens", 0),
    )
    return {key: int(usage.get(key) or 0) for key in USAGE_KEYS}


def subtract_usage(end: dict[str, int] | None, start: dict[str, int] | None) -> dict[str, int] | None:
    if not end or not start:
        return None
    delta = {key: max(0, int(end.get(key, 0)) - int(start.get(key, 0))) for key in USAGE_KEYS}
    delta["uncached_input_tokens"] = max(
        0,
        delta.get("input_tokens", 0) - delta.get("cached_input_tokens", 0),
    )
    return delta


def env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def cost_policy() -> dict[str, Any]:
    hours = {
        level: env_float(f"AGENT_TRADITIONAL_HOURS_{level.upper()}", default, minimum=0.0)
        for level, default in DEFAULT_TRADITIONAL_HOURS.items()
    }
    return {
        "currency": "CNY",
        "usd_to_cny": env_float("AGENT_USD_TO_CNY", DEFAULT_USD_TO_CNY, minimum=0.000001),
        "usd_to_cny_source": "AGENT_USD_TO_CNY or project default",
        "engineer_hourly_rate_cny": env_float(
            "AGENT_ENGINEER_HOURLY_RATE_CNY",
            DEFAULT_ENGINEER_HOURLY_RATE_CNY,
            minimum=0.0,
        ),
        "traditional_hours_by_complexity": hours,
        "model_price_source": "OpenAI API pricing, standard processing, USD per 1M tokens",
        "model_prices_usd_per_1m": MODEL_PRICES_USD_PER_1M,
        "model_price_overrides": "edit MODEL_PRICES_USD_PER_1M, set project-specific hook policy, or adjust generated HTML controls",
    }


def round_money(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def model_price(model: str | None) -> tuple[str | None, dict[str, float | None] | None]:
    normalized = str(model or "").strip().lower()
    if not normalized:
        return None, None
    if normalized in MODEL_PRICES_USD_PER_1M:
        return normalized, MODEL_PRICES_USD_PER_1M[normalized]
    for prefix, key in MODEL_PRICE_PREFIXES:
        if normalized.startswith(prefix):
            return key, MODEL_PRICES_USD_PER_1M[key]
    return None, None


def estimate_turn_economics(
    model: str | None,
    turn_usage: dict[str, int] | None,
    complexity_level: str | None,
    complexity_reason: str | None = None,
) -> dict[str, Any]:
    policy = cost_policy()
    usage = normalize_usage(turn_usage) or {key: 0 for key in USAGE_KEYS}
    input_tokens = int(usage.get("input_tokens") or 0)
    cached_input_tokens = min(input_tokens, int(usage.get("cached_input_tokens") or 0))
    uncached_input_tokens = max(0, input_tokens - cached_input_tokens)
    output_tokens = int(usage.get("output_tokens") or 0)

    price_key, price = model_price(model)
    ai_cost_usd: float | None = None
    input_cost_usd: float | None = None
    cached_input_cost_usd: float | None = None
    output_cost_usd: float | None = None

    if price:
        input_rate = float(price["input"] or 0.0)
        cached_rate = price.get("cached_input")
        output_rate = float(price["output"] or 0.0)
        if cached_rate is None:
            input_cost_usd = (input_tokens / 1_000_000) * input_rate
            cached_input_cost_usd = 0.0
        else:
            input_cost_usd = (uncached_input_tokens / 1_000_000) * input_rate
            cached_input_cost_usd = (cached_input_tokens / 1_000_000) * float(cached_rate)
        output_cost_usd = (output_tokens / 1_000_000) * output_rate
        ai_cost_usd = input_cost_usd + cached_input_cost_usd + output_cost_usd

    level = str(complexity_level or "unknown")
    hours_by_complexity = policy["traditional_hours_by_complexity"]
    traditional_hours = None if level == "unknown" else float(hours_by_complexity.get(level, hours_by_complexity["unknown"]))
    traditional_cost_cny = (
        traditional_hours * float(policy["engineer_hourly_rate_cny"])
        if traditional_hours is not None
        else None
    )
    ai_cost_cny = ai_cost_usd * float(policy["usd_to_cny"]) if ai_cost_usd is not None else None
    replacement_savings_cny = (
        traditional_cost_cny - ai_cost_cny
        if ai_cost_cny is not None and traditional_cost_cny is not None
        else None
    )
    roi_ratio = (
        replacement_savings_cny / ai_cost_cny
        if replacement_savings_cny is not None and ai_cost_cny is not None and ai_cost_cny > 0
        else None
    )

    return {
        "currency": "CNY",
        "pricing_available": bool(price),
        "model_price_key": price_key,
        "usd_to_cny": policy["usd_to_cny"],
        "input_usd_per_1m": price.get("input") if price else None,
        "cached_input_usd_per_1m": price.get("cached_input") if price else None,
        "output_usd_per_1m": price.get("output") if price else None,
        "ai_cost_usd": round_money(ai_cost_usd, 6),
        "ai_cost_cny": round_money(ai_cost_cny, 4),
        "input_cost_cny": round_money(
            (input_cost_usd + cached_input_cost_usd) * float(policy["usd_to_cny"])
            if input_cost_usd is not None and cached_input_cost_usd is not None
            else None,
            4,
        ),
        "uncached_input_cost_cny": round_money(
            input_cost_usd * float(policy["usd_to_cny"]) if input_cost_usd is not None else None,
            4,
        ),
        "cached_input_cost_cny": round_money(
            cached_input_cost_usd * float(policy["usd_to_cny"]) if cached_input_cost_usd is not None else None,
            4,
        ),
        "output_cost_cny": round_money(
            output_cost_usd * float(policy["usd_to_cny"]) if output_cost_usd is not None else None,
            4,
        ),
        "ai_cost_breakdown_usd": {
            "uncached_input": round_money(input_cost_usd, 6),
            "cached_input": round_money(cached_input_cost_usd, 6),
            "output": round_money(output_cost_usd, 6),
        },
        "traditional_complexity": level,
        "traditional_complexity_reason": complexity_reason,
        "traditional_hours": round_money(traditional_hours, 3),
        "traditional_hourly_rate_cny": round_money(float(policy["engineer_hourly_rate_cny"]), 2),
        "traditional_cost_cny": round_money(traditional_cost_cny, 2),
        "replacement_savings_cny": round_money(replacement_savings_cny, 4),
        "roi_ratio": round_money(roi_ratio, 4),
        "roi_percent": round_money(roi_ratio * 100 if roi_ratio is not None else None, 2),
    }


def estimate_text_tokens(text: str | None, model: str | None) -> dict[str, Any]:
    text = text or ""
    if not text:
        return {"tokens": 0, "method": "empty"}

    try:
        import tiktoken  # type: ignore[import-not-found]

        try:
            encoding = tiktoken.encoding_for_model(model or "")
            method = "tiktoken_model"
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")
            method = "tiktoken_cl100k_base"
        return {"tokens": len(encoding.encode(text)), "method": method}
    except Exception:
        cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        ascii_chars = len(text) - cjk_chars
        estimate = int(math.ceil((cjk_chars * 1.2) + (ascii_chars / 4)))
        return {"tokens": max(1, estimate), "method": "local_cjk_ascii_estimate"}


def latest_token_count(transcript_path: str | None) -> dict[str, Any] | None:
    if not transcript_path:
        return None
    path = Path(transcript_path)
    if not path.exists():
        return None

    latest: dict[str, Any] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = item.get("payload") if isinstance(item, dict) else None
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                latest = {
                    "timestamp": item.get("timestamp"),
                    "line_number": line_number,
                    "total_token_usage": normalize_usage(info.get("total_token_usage")),
                    "last_token_usage": normalize_usage(info.get("last_token_usage")),
                    "model_context_window": info.get("model_context_window"),
                }
    except OSError:
        return None
    return latest


def latest_token_count_with_retry(transcript_path: str | None) -> dict[str, Any] | None:
    latest = None
    for _ in range(4):
        latest = latest_token_count(transcript_path)
        if latest:
            return latest
        time.sleep(0.25)
    return latest


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def sanitize_public_task_text(text: str | None, limit: int = 160) -> str:
    raw = text or ""
    lower = raw.lower()
    if "smoke test" in lower and ("hook" in lower or "只回复" in raw):
        return "Codex hook smoke test"

    sanitized = re.sub(r"```.*?```", " [code] ", raw, flags=re.DOTALL)
    sanitized = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", sanitized)
    sanitized = re.sub(r"`([^`]{1,120})`", r"\1", sanitized)
    sanitized = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|authorization)\b\s*[:=]\s*['\"]?[^\s,'\"]+",
        r"\1=[redacted]",
        sanitized,
    )
    sanitized = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", sanitized)
    sanitized = re.sub(r"\b[A-Za-z0-9_./+=-]{48,}\b", "[redacted]", sanitized)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return truncate_text(sanitized or "未命名任务", limit)


def first_statement(text: str | None) -> str:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().strip("-*` ")
        if not line:
            continue
        parts = re.split(r"(?<=[。.!?？])\s+", line, maxsplit=1)
        return parts[0].strip()
    return ""


def looks_like_control_output(text: str | None) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return True
    if normalized.startswith("HOOK_SMOKE_OK"):
        return True
    if normalized in {"好", "好的", "继续", "继续吧", "改吧", "修改吧", "提交", "提交吧", "推送", "推送吧"}:
        return True
    return False


def public_task_description(record: dict[str, Any]) -> dict[str, str]:
    task = record.get("task") if isinstance(record.get("task"), dict) else {}
    git = record.get("git") if isinstance(record.get("git"), dict) else {}
    last_commit = git.get("last_commit") if isinstance(git.get("last_commit"), dict) else {}
    ai_description = str(task.get("description") or "")
    assistant_summary = first_statement(((record.get("assistant_output") or {}).get("text")))
    commit_subject = str(last_commit.get("subject") or "")

    candidates: list[tuple[str, str]] = []
    if ai_description:
        candidates.append((ai_description, str(task.get("description_source") or "ai_summary")))
    if commit_subject and (git.get("head_changed") or git.get("git_closed_loop")):
        candidates.append((commit_subject, "git_commit_subject"))
    if assistant_summary and not looks_like_control_output(assistant_summary):
        candidates.append((assistant_summary, "assistant_result_summary"))
    if commit_subject:
        candidates.append((commit_subject, "git_commit_subject"))

    for candidate, source in candidates:
        description = sanitize_public_task_text(candidate)
        if description:
            return {"description": description, "description_source": source}
    return {"description": "未提供 AI 任务摘要", "description_source": "ai_summary_missing"}


def normalize_complexity(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in COMPLEXITY_LEVELS else "unknown"


def normalize_project_task_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in PROJECT_TASK_STATUSES else "completed"


def normalize_project_task_category(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in PROJECT_TASK_CATEGORIES else "other"


def parse_bool_flag(value: Any, *, default: bool | None = None) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def stable_task_id(value: str | None, fallback: str | None = None) -> str:
    raw = (value or fallback or "ai-task").strip().lower()
    raw = re.sub(r"[^a-z0-9]+", "-", raw)
    raw = re.sub(r"-+", "-", raw).strip("-")
    if raw:
        return truncate_text(raw, 80)
    digest = hashlib.sha1((value or fallback or "ai-task").encode("utf-8")).hexdigest()[:10]
    return f"ai-task-{digest}"


def parse_turn_index_list(value: str | None) -> list[int]:
    if not value:
        return []
    indices: list[int] = []
    for part in re.split(r"[,\s]+", value.strip()):
        if not part:
            continue
        try:
            parsed = int(part)
        except ValueError:
            continue
        if parsed > 0 and parsed not in indices:
            indices.append(parsed)
    return indices


def public_task_complexity(record: dict[str, Any]) -> dict[str, str | None]:
    task = record.get("task") if isinstance(record.get("task"), dict) else {}
    source = str(task.get("complexity_source") or "")
    if source.startswith("ai") or source == "manual":
        return {
            "complexity": normalize_complexity(task.get("complexity")),
            "complexity_reason": task.get("complexity_reason"),
            "complexity_source": source,
        }
    return {
        "complexity": "unknown",
        "complexity_reason": "ai_assessment_missing",
        "complexity_source": "missing",
    }


def event_ai_task_metadata(event: dict[str, Any]) -> dict[str, Any]:
    raw_task = event.get("ai_task") if isinstance(event.get("ai_task"), dict) else {}
    description = (
        raw_task.get("description")
        or event.get("task_description")
        or event.get("ai_task_description")
    )
    complexity = (
        raw_task.get("complexity")
        or event.get("task_complexity")
        or event.get("ai_task_complexity")
    )
    reason = (
        raw_task.get("complexity_reason")
        or event.get("task_complexity_reason")
        or event.get("ai_task_complexity_reason")
    )
    return {
        "description": sanitize_public_task_text(str(description)) if description else "",
        "description_source": "ai_event_metadata" if description else "ai_summary_missing",
        "complexity": normalize_complexity(complexity),
        "complexity_reason": str(reason) if reason else None,
        "complexity_source": "ai_event_metadata" if complexity else "missing",
    }


def pending_ai_task_metadata(pending: dict[str, Any]) -> dict[str, Any]:
    raw_task = pending.get("ai_task_metadata") if isinstance(pending.get("ai_task_metadata"), dict) else {}
    description = raw_task.get("description")
    complexity = raw_task.get("complexity")
    reason = raw_task.get("complexity_reason")
    return {
        "description": sanitize_public_task_text(str(description)) if description else "",
        "description_source": str(raw_task.get("description_source") or "ai_pending_metadata") if description else "ai_summary_missing",
        "complexity": normalize_complexity(complexity),
        "complexity_reason": str(reason) if reason else None,
        "complexity_source": str(raw_task.get("complexity_source") or "ai_pending_metadata") if complexity else "missing",
    }


def turn_ai_task_metadata(
    event: dict[str, Any],
    pending: dict[str, Any],
    assistant_message: str | None,
) -> dict[str, Any]:
    event_metadata = event_ai_task_metadata(event)
    pending_metadata = pending_ai_task_metadata(pending)
    description = event_metadata["description"] or pending_metadata["description"]
    description_source = (
        event_metadata["description_source"]
        if event_metadata["description"]
        else pending_metadata["description_source"]
    )
    complexity = (
        event_metadata["complexity"]
        if event_metadata["complexity"] != "unknown"
        else pending_metadata["complexity"]
    )
    complexity_reason = (
        event_metadata["complexity_reason"]
        if event_metadata["complexity"] != "unknown"
        else pending_metadata["complexity_reason"]
    )
    complexity_source = (
        event_metadata["complexity_source"]
        if event_metadata["complexity"] != "unknown"
        else pending_metadata["complexity_source"]
    )

    if not description:
        assistant_summary = first_statement(assistant_message)
        if assistant_summary and not looks_like_control_output(assistant_summary):
            description = sanitize_public_task_text(assistant_summary)
            description_source = "assistant_result_summary"
    if not description:
        description = "未提供 AI 任务摘要"
        description_source = "ai_summary_missing"

    return {
        "description": description,
        "description_source": description_source,
        "complexity": normalize_complexity(complexity),
        "complexity_reason": complexity_reason or "ai_assessment_missing",
        "complexity_source": complexity_source if complexity != "unknown" else "missing",
    }


def line_count(text: str | None) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def git_status_lines(root: Path) -> list[str]:
    status = run_text(["git", "status", "--porcelain"], root)
    return status.splitlines() if status else []


def git_snapshot(root: Path, started_at: str | None, start_git: dict[str, Any] | None = None) -> dict[str, Any]:
    status_lines = git_status_lines(root)
    head = run_text(["git", "rev-parse", "--short", "HEAD"], root)
    commit_raw = run_text(
        ["git", "log", "-1", "--format=%cI%x00%H%x00%an%x00%ae%x00%s"],
        root,
    )

    commit: dict[str, Any] | None = None
    commit_time = None
    if commit_raw:
        parts = commit_raw.split("\x00", 4)
        if len(parts) == 5:
            commit_time = parse_time(parts[0])
            commit = {
                "committed_at": parts[0],
                "sha": parts[1],
                "author_name": parts[2],
                "author_email": parts[3],
                "subject": parts[4],
            }

    start_time = parse_time(started_at)
    start_status_lines = []
    if isinstance(start_git, dict) and isinstance(start_git.get("status_lines"), list):
        start_status_lines = [str(line) for line in start_git.get("status_lines") or []]
    start_status_set = set(start_status_lines)
    status_delta_lines = [line for line in status_lines if line not in start_status_set]

    clean = len(status_lines) == 0
    git_closed_loop = bool(clean and start_time and commit_time and commit_time >= start_time)
    return {
        "clean": clean,
        "git_closed_loop": git_closed_loop,
        "head": head,
        "head_changed": bool(start_git and start_git.get("head") != head),
        "last_commit": commit,
        "start_head": (start_git or {}).get("head") if isinstance(start_git, dict) else None,
        "start_status_count": len(start_status_lines),
        "status_count": len(status_lines),
        "status_sample": status_lines[:20],
        "status_delta_count": len(status_delta_lines),
        "status_delta_sample": status_delta_lines[:20],
    }


def latest_git_commit(root: Path) -> dict[str, Any] | None:
    commit_raw = run_text(
        ["git", "log", "-1", "--format=%cI%x00%H%x00%an%x00%ae%x00%s"],
        root,
    )
    if not commit_raw:
        return None
    parts = commit_raw.split("\x00", 4)
    if len(parts) != 5:
        return None
    return {
        "committed_at": parts[0],
        "sha": parts[1],
        "author_name": parts[2],
        "author_email": parts[3],
        "subject": parts[4],
    }


class UsageLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "UsageLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def read_records(records_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not records_path.exists():
        return records
    try:
        with records_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        return records
    return records


def public_history_item(index: int, record: dict[str, Any]) -> dict[str, Any]:
    task = record.get("task") if isinstance(record.get("task"), dict) else {}
    timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
    git = record.get("git") if isinstance(record.get("git"), dict) else {}
    description = public_task_description(record)
    complexity = public_task_complexity(record)
    return {
        "turn_index": index,
        "recorded_at": record.get("recorded_at"),
        "model": record.get("model") or "unknown",
        "task": {
            "description": description["description"],
            "description_source": description["description_source"],
            "complexity": complexity["complexity"],
            "complexity_reason": complexity["complexity_reason"],
            "complexity_source": complexity["complexity_source"],
        },
        "timing": {
            "elapsed_seconds": timing.get("elapsed_seconds"),
        },
        "token_usage": normalize_usage(((record.get("token_usage") or {}).get("turn") or {}))
        or {key: 0 for key in USAGE_KEYS},
        "git": {
            "git_closed_loop": bool(git.get("git_closed_loop")),
            "head_changed": bool(git.get("head_changed")),
            "status_delta_count": int(git.get("status_delta_count") or 0),
        },
    }


def empty_usage_totals() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


def add_usage_totals(totals: dict[str, int], usage: dict[str, Any] | None) -> None:
    normalized = normalize_usage(usage) or empty_usage_totals()
    for key in USAGE_KEYS:
        totals[key] += int(normalized.get(key) or 0)


def project_task_metrics(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    token_totals = empty_usage_totals()
    complexity_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    models: dict[str, int] = {}
    elapsed_total = 0.0
    git_closed_loops = 0

    for item in tasks:
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        timing = item.get("timing") if isinstance(item.get("timing"), dict) else {}
        git = item.get("git") if isinstance(item.get("git"), dict) else {}
        add_usage_totals(token_totals, item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {})
        elapsed_total += float(timing.get("elapsed_seconds") or 0.0)
        complexity = normalize_complexity(task.get("complexity"))
        category = normalize_project_task_category(task.get("category"))
        status = normalize_project_task_status(task.get("status"))
        complexity_counts[complexity] = complexity_counts.get(complexity, 0) + 1
        category_counts[category] = category_counts.get(category, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        if git.get("git_closed_loop"):
            git_closed_loops += 1
        task_models = item.get("models") if isinstance(item.get("models"), dict) else {}
        for model, count in task_models.items():
            models[str(model)] = models.get(str(model), 0) + int(count or 0)

    return {
        "recorded_tasks": len(tasks),
        "status_counts": status_counts,
        "complexity_counts": complexity_counts,
        "category_counts": category_counts,
        "git_closed_loops": git_closed_loops,
        "token_totals": token_totals,
        "elapsed_seconds_total": round(elapsed_total, 3),
        "turns_by_model": models,
    }


def empty_project_summary(root: Path) -> dict[str, Any]:
    return {
        "schema_version": PROJECT_SUMMARY_SCHEMA_VERSION,
        "project": root.name,
        "updated_at": None,
        "metric_layers": {
            "local_usage_files": "turn-level data is stored in ignored .agent/usage/summary.json and codex-turns.jsonl",
            "task_history": "background-AI-curated project value tasks; no one-turn one-task mapping",
        },
        "task_metrics": project_task_metrics([]),
        "task_history": [],
        "privacy": {
            "contains_user_prompts": False,
            "contains_assistant_outputs": False,
            "contains_session_ids": False,
            "contains_transcript_paths": False,
            "contains_derived_costs": False,
            "contains_roi": False,
            "contains_turn_level_task_history": False,
            "task_history_is_ai_curated": True,
            "task_history_maintainer": "background Codex exec agent, with manual override command",
            "source_detail_file": ".agent/usage/codex-turns.jsonl",
            "source_detail_file_committed": False,
        },
        "notes": [
            "hook records turn-level token, timing, model, and git metadata into ignored local files",
            "project task history is updated by a background AI maintainer using a normalized schema",
            "consulting-only, git-only, and other no-deliverable turns can remain local-only without task entries",
            "cost and ROI are regenerated into a separate value report from this summary and current policy",
        ],
    }


def load_project_summary(root: Path, out_dir: Path) -> dict[str, Any]:
    existing = load_json(out_dir / "project-summary.json")
    if not isinstance(existing, dict) or existing.get("schema_version") != PROJECT_SUMMARY_SCHEMA_VERSION:
        return empty_project_summary(root)
    return existing


def write_project_summary(
    root: Path,
    out_dir: Path,
    project_summary: dict[str, Any],
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if summary is None:
        summary = load_json(out_dir / "summary.json")
    tasks = project_summary.get("task_history") if isinstance(project_summary.get("task_history"), list) else []
    finalized = empty_project_summary(root)
    finalized.update(
        {
            "updated_at": utc_now(),
            "task_metrics": project_task_metrics([task for task in tasks if isinstance(task, dict)]),
            "task_history": tasks,
        }
    )
    write_json(out_dir / "project-summary.json", finalized)
    return finalized


def parse_project_task_update_args(argv: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    values: dict[str, Any] = {
        "status": "completed",
        "category": "implementation",
        "complexity": "unknown",
        "include_current_turn": True,
        "apply_now": False,
        "replace_turns": False,
        "turn_indices": [],
        "from_turn": None,
        "to_turn": None,
        "git_closed_loop": None,
    }
    value_options = {
        "--task-id": "task_id",
        "--title": "title",
        "--summary": "summary",
        "--status": "status",
        "--category": "category",
        "--complexity": "complexity",
        "--reason": "complexity_reason",
        "--business-value": "business_value",
        "--turns": "turns",
        "--from-turn": "from_turn",
        "--to-turn": "to_turn",
        "--git-closed-loop": "git_closed_loop_raw",
    }

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--include-current-turn":
            values["include_current_turn"] = True
            index += 1
            continue
        if arg == "--no-current-turn":
            values["include_current_turn"] = False
            index += 1
            continue
        if arg == "--apply-now":
            values["apply_now"] = True
            index += 1
            continue
        if arg == "--replace-turns":
            values["replace_turns"] = True
            index += 1
            continue
        key = value_options.get(arg)
        if key is None:
            return None, f"unknown option: {arg}"
        if index + 1 >= len(argv):
            return None, f"missing value for {arg}"
        values[key] = argv[index + 1]
        index += 2

    title = sanitize_public_task_text(values.get("title") or "")
    if not title and not values.get("task_id"):
        return None, "--title or --task-id is required"

    for int_key in ("from_turn", "to_turn"):
        if values.get(int_key) is None:
            continue
        try:
            parsed = int(values[int_key])
        except (TypeError, ValueError):
            return None, f"{int_key.replace('_', '-')} must be an integer"
        values[int_key] = parsed if parsed > 0 else None

    git_closed_loop_raw = values.pop("git_closed_loop_raw", None)
    values["git_closed_loop"] = parse_bool_flag(git_closed_loop_raw, default=None)
    values["turn_indices"] = parse_turn_index_list(values.pop("turns", None))
    values["task_id"] = stable_task_id(values.get("task_id"), title)
    values["title"] = title or values["task_id"]
    values["summary"] = sanitize_public_task_text(values.get("summary") or values["title"], 500)
    values["status"] = normalize_project_task_status(values.get("status"))
    values["category"] = normalize_project_task_category(values.get("category"))
    values["complexity"] = normalize_complexity(values.get("complexity"))
    values["complexity_reason"] = (
        sanitize_public_task_text(values.get("complexity_reason"), 240)
        if values.get("complexity_reason")
        else "ai_assessment_missing"
    )
    values["business_value"] = (
        sanitize_public_task_text(values.get("business_value"), 500)
        if values.get("business_value")
        else ""
    )
    return values, None


def select_project_task_turns(
    records: list[dict[str, Any]],
    metadata: dict[str, Any],
    current_record_id: str | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    selected_indices = set(int(index) for index in metadata.get("turn_indices") or [] if int(index) > 0)
    from_turn = metadata.get("from_turn")
    to_turn = metadata.get("to_turn")
    if from_turn or to_turn:
        start = int(from_turn or 1)
        end = int(to_turn or len(records))
        for index in range(max(1, start), min(len(records), end) + 1):
            selected_indices.add(index)

    if metadata.get("include_current_turn") and current_record_id:
        for index, record in enumerate(records, 1):
            if record.get("record_id") == current_record_id:
                selected_indices.add(index)
                break

    return [
        (index, record)
        for index, record in enumerate(records, 1)
        if index in selected_indices
    ]


def aggregate_project_task_turns(selected: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    token_totals = empty_usage_totals()
    elapsed_seconds = 0.0
    models: dict[str, int] = {}
    first_recorded_at = None
    last_recorded_at = None
    git_closed_loop = False
    last_commit: dict[str, Any] | None = None

    for _index, record in selected:
        usage = ((record.get("token_usage") or {}).get("turn") or {})
        add_usage_totals(token_totals, usage if isinstance(usage, dict) else {})
        timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
        elapsed_seconds += float(timing.get("elapsed_seconds") or 0.0)
        model = str(record.get("model") or "unknown")
        models[model] = models.get(model, 0) + 1
        recorded_at = record.get("recorded_at")
        first_recorded_at = first_recorded_at or recorded_at
        last_recorded_at = recorded_at or last_recorded_at
        git = record.get("git") if isinstance(record.get("git"), dict) else {}
        if git.get("git_closed_loop"):
            git_closed_loop = True
        commit = git.get("last_commit") if isinstance(git.get("last_commit"), dict) else None
        if commit and git.get("git_closed_loop"):
            last_commit = commit

    return {
        "turn_indices": [index for index, _record in selected],
        "turn_count": len(selected),
        "first_recorded_at": first_recorded_at,
        "last_recorded_at": last_recorded_at,
        "token_usage": token_totals,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "models": models,
        "git_closed_loop": git_closed_loop,
        "last_commit": last_commit,
    }


def upsert_project_task(
    project_summary: dict[str, Any],
    metadata: dict[str, Any],
    aggregate: dict[str, Any],
) -> dict[str, Any]:
    tasks = project_summary.get("task_history") if isinstance(project_summary.get("task_history"), list) else []
    task_id = metadata["task_id"]
    existing_index = next(
        (index for index, item in enumerate(tasks) if isinstance(item, dict) and item.get("task_id") == task_id),
        None,
    )
    existing = tasks[existing_index] if existing_index is not None else {}
    existing_created_at = existing.get("created_at") if isinstance(existing, dict) else None
    commit = aggregate.get("last_commit") if isinstance(aggregate.get("last_commit"), dict) else None
    git_closed_loop = (
        bool(metadata["git_closed_loop"])
        if metadata.get("git_closed_loop") is not None
        else bool(aggregate.get("git_closed_loop"))
    )
    git_closed_loop = bool(git_closed_loop and commit)
    task_item = {
        "task_id": task_id,
        "created_at": existing_created_at or utc_now(),
        "updated_at": utc_now(),
        "source": "ai_agent_curated",
        "task": {
            "title": metadata["title"],
            "summary": metadata["summary"],
            "status": metadata["status"],
            "category": metadata["category"],
            "complexity": metadata["complexity"],
            "complexity_reason": metadata["complexity_reason"],
            "complexity_source": "ai_project_task_metadata",
            "business_value": metadata["business_value"],
        },
        "turns": {
            "turn_indices": aggregate["turn_indices"],
            "turn_count": aggregate["turn_count"],
            "first_recorded_at": aggregate["first_recorded_at"],
            "last_recorded_at": aggregate["last_recorded_at"],
        },
        "models": aggregate["models"],
        "timing": {
            "elapsed_seconds": aggregate["elapsed_seconds"],
        },
        "token_usage": aggregate["token_usage"],
        "git": {
            "git_closed_loop": git_closed_loop,
            "commit_sha": commit.get("sha") if commit and git_closed_loop else None,
            "commit_subject": commit.get("subject") if commit and git_closed_loop else None,
            "committed_at": commit.get("committed_at") if commit and git_closed_loop else None,
        },
    }
    if existing_index is None:
        tasks.append(task_item)
    else:
        tasks[existing_index] = task_item
    project_summary["task_history"] = tasks
    return project_summary


def apply_project_task_update(
    root: Path,
    out_dir: Path,
    metadata: dict[str, Any],
    current_record_id: str | None = None,
) -> dict[str, Any]:
    records = read_records(out_dir / "codex-turns.jsonl")
    summary = load_json(out_dir / "summary.json") or rebuild_summary(root, out_dir)
    project_summary = load_project_summary(root, out_dir)
    if not metadata.get("replace_turns"):
        existing_tasks = project_summary.get("task_history") if isinstance(project_summary.get("task_history"), list) else []
        existing = next(
            (
                item
                for item in existing_tasks
                if isinstance(item, dict) and item.get("task_id") == metadata.get("task_id")
            ),
            None,
        )
        existing_turns = ((existing or {}).get("turns") or {}).get("turn_indices") if isinstance(existing, dict) else []
        if isinstance(existing_turns, list):
            merged = set(int(index) for index in metadata.get("turn_indices") or [])
            for index in existing_turns:
                try:
                    parsed = int(index)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    merged.add(parsed)
            metadata = dict(metadata)
            metadata["turn_indices"] = sorted(merged)
    selected = select_project_task_turns(records, metadata, current_record_id)
    aggregate = aggregate_project_task_turns(selected)
    project_summary = upsert_project_task(project_summary, metadata, aggregate)
    return write_project_summary(root, out_dir, project_summary, summary)


def record_project_task(root: Path, out_dir: Path, argv: list[str]) -> int:
    metadata, error = parse_project_task_update_args(argv)
    if error or metadata is None:
        print(error or "invalid project task metadata", file=sys.stderr)
        return 2

    pending_path = newest_pending_path(out_dir)
    if pending_path is not None and not metadata.get("apply_now"):
        pending = load_json(pending_path) or {}
        pending["project_task_update"] = metadata
        write_json(pending_path, pending)
        print(json.dumps({"ok": True, "mode": "queued_for_stop", "pending_path": str(pending_path)}, ensure_ascii=False, sort_keys=True))
        return 0

    project_summary = apply_project_task_update(root, out_dir, metadata)
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "applied",
                "project_summary": str(out_dir / "project-summary.json"),
                "recorded_tasks": project_summary.get("task_metrics", {}).get("recorded_tasks"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def maintainer_enabled() -> bool:
    if parse_bool_flag(os.environ.get(MAINTAINER_ENV_FLAG), default=True) is False:
        return False
    if parse_bool_flag(os.environ.get(MAINTAINER_ACTIVE_ENV), default=False):
        return False
    return True


def maintainer_dir(out_dir: Path) -> Path:
    return out_dir / "project-summary-agent"


def maintainer_log(out_dir: Path) -> Path:
    return maintainer_dir(out_dir) / "jobs.jsonl"


def log_maintainer_event(out_dir: Path, event: dict[str, Any]) -> None:
    payload = {"timestamp": utc_now(), **event}
    try:
        append_jsonl(maintainer_log(out_dir), payload)
    except OSError:
        pass


def maintainer_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "decisions"],
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action", "reason"],
                    "properties": {
                        "action": {"type": "string", "enum": ["skip_turn", "upsert_task"]},
                        "reason": {"type": "string"},
                        "task_id": {"type": "string"},
                        "title": {"type": "string"},
                        "summary": {"type": "string"},
                        "status": {"type": "string", "enum": sorted(PROJECT_TASK_STATUSES)},
                        "category": {"type": "string", "enum": sorted(PROJECT_TASK_CATEGORIES)},
                        "complexity": {"type": "string", "enum": sorted(COMPLEXITY_LEVELS)},
                        "complexity_reason": {"type": "string"},
                        "business_value": {"type": "string"},
                        "turn_indices": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "git_closed_loop": {"type": ["boolean", "null"]},
                        "replace_turns": {"type": "boolean"},
                    },
                },
            },
        },
    }


def record_for_maintainer(index: int, record: dict[str, Any]) -> dict[str, Any]:
    task = record.get("task") if isinstance(record.get("task"), dict) else {}
    timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
    git = record.get("git") if isinstance(record.get("git"), dict) else {}
    last_commit = git.get("last_commit") if isinstance(git.get("last_commit"), dict) else None
    return {
        "turn_index": index,
        "recorded_at": record.get("recorded_at"),
        "model": record.get("model") or "unknown",
        "user_prompt_summary": sanitize_public_task_text(((record.get("user_input") or {}).get("text")), 500),
        "assistant_result_summary": sanitize_public_task_text(((record.get("assistant_output") or {}).get("text")), 500),
        "turn_task_hint": {
            "description": task.get("description"),
            "description_source": task.get("description_source"),
            "complexity": task.get("complexity"),
            "complexity_reason": task.get("complexity_reason"),
        },
        "timing": {
            "elapsed_seconds": timing.get("elapsed_seconds"),
        },
        "token_usage": normalize_usage(((record.get("token_usage") or {}).get("turn") or {}))
        or empty_usage_totals(),
        "git": {
            "clean": bool(git.get("clean")),
            "head_changed": bool(git.get("head_changed")),
            "git_closed_loop": bool(git.get("git_closed_loop")),
            "status_delta_count": int(git.get("status_delta_count") or 0),
            "status_delta_sample": [str(line) for line in git.get("status_delta_sample") or []][:20],
            "last_commit": (
                {
                    "sha": last_commit.get("sha"),
                    "committed_at": last_commit.get("committed_at"),
                    "subject": last_commit.get("subject"),
                    "author_name": last_commit.get("author_name"),
                    "author_email": last_commit.get("author_email"),
                }
                if last_commit
                else None
            ),
        },
    }


def project_summary_for_maintainer(project_summary: dict[str, Any]) -> dict[str, Any]:
    tasks = project_summary.get("task_history") if isinstance(project_summary.get("task_history"), list) else []
    return {
        "schema_version": project_summary.get("schema_version"),
        "task_metrics": project_summary.get("task_metrics"),
        "task_history": tasks,
    }


def build_maintainer_prompt(root: Path, out_dir: Path, record_id: str | None) -> tuple[str, int | None]:
    records = read_records(out_dir / "codex-turns.jsonl")
    current_index = None
    if record_id:
        for index, record in enumerate(records, 1):
            if record.get("record_id") == record_id:
                current_index = index
                break
    if current_index is None and records:
        current_index = len(records)

    recent_start = max(1, (current_index or len(records) or 1) - 8)
    recent_records = [
        record_for_maintainer(index, record)
        for index, record in enumerate(records, 1)
        if index >= recent_start
    ]
    current_record = (
        record_for_maintainer(current_index, records[current_index - 1])
        if current_index and 0 < current_index <= len(records)
        else None
    )
    project_summary = load_project_summary(root, out_dir)
    context = {
        "project": root.name,
        "current_turn_index": current_index,
        "current_record": current_record,
        "recent_turn_records": recent_records,
        "project_summary": project_summary_for_maintainer(project_summary),
        "current_git_status": {
            "head": run_text(["git", "rev-parse", "--short", "HEAD"], root),
            "status_lines": git_status_lines(root)[:40],
        },
    }
    prompt = f"""You are a background AI maintainer for `.agent/usage/project-summary.json`.

Your job is to decide whether the latest Codex turn should update the AI-curated task history.
Do not edit files. Do not run shell commands. Return only JSON matching the supplied schema.

Rules:
- Turn-level metrics are stored in ignored local files; you only decide `task_history` updates.
- Do not create a task for consultation-only, status-only, environment inspection, pure git commit, pure push, or any turn with no actual deliverable.
- Do not create a task named like "commit git" or "push". If a git-only turn closes earlier work, update the existing task and set `git_closed_loop=true`.
- If the latest turn is part of an existing task, return `upsert_task` with that same `task_id` and all turn indices that belong to the task. Reuse existing task wording when it is still accurate.
- If a task spans multiple user turns, merge them into one task. Never create duplicate tasks for the same work.
- If the latest turn is already represented in `project_summary.task_history`, return `skip_turn`.
- Use stable fields only. Do not include raw prompts, assistant output, session ids, transcript paths, local absolute paths, costs, or ROI.
- Allowed categories: {", ".join(sorted(PROJECT_TASK_CATEGORIES))}.
- Allowed complexity values: low, medium, high, unknown.

Context:
```json
{json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)}
```
"""
    return prompt, current_index


def decision_turn_indices(decision: dict[str, Any], current_turn_index: int | None) -> list[int]:
    raw = decision.get("turn_indices")
    indices: list[int] = []
    if isinstance(raw, list):
        for value in raw:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0 and parsed not in indices:
                indices.append(parsed)
    if not indices and current_turn_index:
        indices.append(current_turn_index)
    return indices


def apply_maintainer_decisions(
    root: Path,
    out_dir: Path,
    result: dict[str, Any],
    current_turn_index: int | None,
) -> dict[str, int]:
    counts = {"upserted": 0, "skipped": 0, "invalid": 0}
    decisions = result.get("decisions") if isinstance(result.get("decisions"), list) else []
    records = read_records(out_dir / "codex-turns.jsonl")
    current_record = (
        records[current_turn_index - 1]
        if current_turn_index and 0 < current_turn_index <= len(records)
        else {}
    )
    current_git = current_record.get("git") if isinstance(current_record.get("git"), dict) else {}
    if not decisions:
        counts["skipped"] += 1
        return counts

    for decision in decisions:
        if not isinstance(decision, dict):
            counts["invalid"] += 1
            continue
        action = str(decision.get("action") or "")
        if action == "skip_turn":
            counts["skipped"] += 1
            continue
        if action != "upsert_task":
            counts["invalid"] += 1
            continue
        title = sanitize_public_task_text(decision.get("title") or decision.get("task_id") or "", 160)
        task_id = stable_task_id(str(decision.get("task_id") or ""), title)
        metadata = {
            "task_id": task_id,
            "title": title or task_id,
            "summary": sanitize_public_task_text(decision.get("summary") or title or task_id, 500),
            "status": normalize_project_task_status(decision.get("status")),
            "category": normalize_project_task_category(decision.get("category")),
            "complexity": normalize_complexity(decision.get("complexity")),
            "complexity_reason": sanitize_public_task_text(
                decision.get("complexity_reason") or decision.get("reason") or "ai_assessment_missing",
                240,
            ),
            "business_value": sanitize_public_task_text(decision.get("business_value") or "", 500),
            "turn_indices": decision_turn_indices(decision, current_turn_index),
            "include_current_turn": False,
            "replace_turns": bool(decision.get("replace_turns")),
            "git_closed_loop": parse_bool_flag(decision.get("git_closed_loop"), default=None),
            "apply_now": True,
            "from_turn": None,
            "to_turn": None,
        }
        if metadata["git_closed_loop"] is True and current_turn_index and current_git.get("git_closed_loop"):
            if current_turn_index not in metadata["turn_indices"]:
                metadata["turn_indices"].append(current_turn_index)
                metadata["turn_indices"] = sorted(set(metadata["turn_indices"]))
        apply_project_task_update(root, out_dir, metadata)
        counts["upserted"] += 1

    return counts


def run_project_summary_agent(root: Path, out_dir: Path, record_id: str | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = maintainer_dir(out_dir)
    agent_dir.mkdir(parents=True, exist_ok=True)
    lock_path = agent_dir / ".lock"
    lock_handle = lock_path.open("a+", encoding="utf-8")
    if fcntl is not None:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log_maintainer_event(out_dir, {"event": "skip_locked", "record_id": record_id})
            lock_handle.close()
            return 0

    job_id = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{safe_name(record_id)}"
    try:
        codex_bin = shutil.which("codex")
        if not codex_bin:
            log_maintainer_event(out_dir, {"event": "missing_codex", "record_id": record_id})
            return 0

        prompt, current_turn_index = build_maintainer_prompt(root, out_dir, record_id)
        schema_path = agent_dir / "project-summary-agent.schema.json"
        prompt_path = agent_dir / f"{job_id}.prompt.md"
        output_path = agent_dir / f"{job_id}.output.json"
        stderr_path = agent_dir / f"{job_id}.stderr.log"
        write_json(schema_path, maintainer_output_schema())
        prompt_path.write_text(prompt, encoding="utf-8")

        env = os.environ.copy()
        env[MAINTAINER_ACTIVE_ENV] = "1"
        env[MAINTAINER_ENV_FLAG] = "0"
        env["AGENT_USAGE_DIR"] = str(out_dir)
        model = os.environ.get(MAINTAINER_MODEL_ENV, DEFAULT_MAINTAINER_MODEL)
        cmd = [
            codex_bin,
            "exec",
            "--ignore-rules",
            "--disable",
            "codex_hooks",
            "--sandbox",
            "read-only",
            "-c",
            'approval_policy="never"',
            "-C",
            str(root),
            "-m",
            model,
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
            "-",
        ]
        log_maintainer_event(out_dir, {"event": "start", "record_id": record_id, "job_id": job_id, "model": model})
        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            result = subprocess.run(
                cmd,
                cwd=str(root),
                env=env,
                input=prompt,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                timeout=int(env_float("AGENT_USAGE_MAINTAINER_TIMEOUT", 600.0, minimum=30.0)),
                check=False,
            )
        if result.returncode != 0:
            log_maintainer_event(
                out_dir,
                {"event": "codex_failed", "record_id": record_id, "job_id": job_id, "returncode": result.returncode},
            )
            return 0

        output = load_json(output_path)
        if not isinstance(output, dict):
            log_maintainer_event(out_dir, {"event": "invalid_output", "record_id": record_id, "job_id": job_id})
            return 0
        counts = apply_maintainer_decisions(root, out_dir, output, current_turn_index)
        log_maintainer_event(
            out_dir,
            {"event": "applied", "record_id": record_id, "job_id": job_id, "counts": counts},
        )
        return 0
    except subprocess.TimeoutExpired:
        log_maintainer_event(out_dir, {"event": "timeout", "record_id": record_id, "job_id": job_id})
        return 0
    except Exception as exc:
        log_maintainer_event(
            out_dir,
            {"event": "error", "record_id": record_id, "job_id": job_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return 0
    finally:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def maybe_start_project_summary_agent(root: Path, out_dir: Path, record_id: str) -> None:
    if not maintainer_enabled():
        log_maintainer_event(out_dir, {"event": "disabled", "record_id": record_id})
        return
    if parse_bool_flag(os.environ.get(MAINTAINER_SYNC_ENV), default=False):
        run_project_summary_agent(root, out_dir, record_id)
        return

    script_path = Path(__file__).resolve()
    env = os.environ.copy()
    env[MAINTAINER_ACTIVE_ENV] = "1"
    env["AGENT_USAGE_DIR"] = str(out_dir)
    command = [
        sys.executable,
        str(script_path),
        "--run-project-summary-agent",
        "--record-id",
        record_id,
    ]
    try:
        agent_dir = maintainer_dir(out_dir)
        agent_dir.mkdir(parents=True, exist_ok=True)
        with (agent_dir / "process.log").open("ab") as process_log:
            subprocess.Popen(
                command,
                cwd=str(root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=process_log,
                stderr=process_log,
                start_new_session=True,
                close_fds=True,
            )
        log_maintainer_event(out_dir, {"event": "spawned", "record_id": record_id})
    except Exception as exc:
        log_maintainer_event(
            out_dir,
            {"event": "spawn_failed", "record_id": record_id, "error": f"{type(exc).__name__}: {exc}"},
        )


def empty_cost_totals() -> dict[str, Any]:
    return {
        "currency": "CNY",
        "priced_turns": 0,
        "unpriced_turns": 0,
        "ai_cost_usd": 0.0,
        "ai_cost_cny": 0.0,
        "input_cost_cny": 0.0,
        "uncached_input_cost_cny": 0.0,
        "cached_input_cost_cny": 0.0,
        "output_cost_cny": 0.0,
        "traditional_cost_cny_all": 0.0,
        "traditional_cost_cny_priced": 0.0,
        "replacement_savings_cny": 0.0,
        "roi_ratio": None,
        "roi_percent": None,
    }


def add_cost_totals(
    totals: dict[str, Any],
    cost_by_model: dict[str, dict[str, Any]],
    model: str,
    economics: dict[str, Any],
) -> None:
    traditional = float(economics.get("traditional_cost_cny") or 0.0)
    totals["traditional_cost_cny_all"] += traditional

    ai_cost_cny = economics.get("ai_cost_cny")
    if ai_cost_cny is None:
        totals["unpriced_turns"] += 1
        return

    totals["priced_turns"] += 1
    totals["ai_cost_usd"] += float(economics.get("ai_cost_usd") or 0.0)
    totals["ai_cost_cny"] += float(ai_cost_cny)
    totals["input_cost_cny"] += float(economics.get("input_cost_cny") or 0.0)
    totals["uncached_input_cost_cny"] += float(economics.get("uncached_input_cost_cny") or 0.0)
    totals["cached_input_cost_cny"] += float(economics.get("cached_input_cost_cny") or 0.0)
    totals["output_cost_cny"] += float(economics.get("output_cost_cny") or 0.0)
    totals["traditional_cost_cny_priced"] += traditional
    totals["replacement_savings_cny"] += float(economics.get("replacement_savings_cny") or 0.0)

    model_entry = cost_by_model.setdefault(
        model,
        {
            "currency": "CNY",
            "turns": 0,
            "ai_cost_cny": 0.0,
            "input_cost_cny": 0.0,
            "uncached_input_cost_cny": 0.0,
            "cached_input_cost_cny": 0.0,
            "output_cost_cny": 0.0,
            "traditional_cost_cny": 0.0,
            "replacement_savings_cny": 0.0,
        },
    )
    model_entry["turns"] += 1
    model_entry["ai_cost_cny"] += float(ai_cost_cny)
    model_entry["input_cost_cny"] += float(economics.get("input_cost_cny") or 0.0)
    model_entry["uncached_input_cost_cny"] += float(economics.get("uncached_input_cost_cny") or 0.0)
    model_entry["cached_input_cost_cny"] += float(economics.get("cached_input_cost_cny") or 0.0)
    model_entry["output_cost_cny"] += float(economics.get("output_cost_cny") or 0.0)
    model_entry["traditional_cost_cny"] += traditional
    model_entry["replacement_savings_cny"] += float(economics.get("replacement_savings_cny") or 0.0)


def finalize_cost_totals(totals: dict[str, Any]) -> dict[str, Any]:
    ai_cost = float(totals.get("ai_cost_cny") or 0.0)
    savings = float(totals.get("replacement_savings_cny") or 0.0)
    roi_ratio = savings / ai_cost if ai_cost > 0 else None
    finalized = dict(totals)
    for key in (
        "ai_cost_usd",
        "ai_cost_cny",
        "input_cost_cny",
        "uncached_input_cost_cny",
        "cached_input_cost_cny",
        "output_cost_cny",
        "traditional_cost_cny_all",
        "traditional_cost_cny_priced",
        "replacement_savings_cny",
    ):
        finalized[key] = round_money(float(finalized.get(key) or 0.0), 4)
    finalized["roi_ratio"] = round_money(roi_ratio, 4)
    finalized["roi_percent"] = round_money(roi_ratio * 100 if roi_ratio is not None else None, 2)
    return finalized


def finalize_cost_by_model(cost_by_model: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for model, values in cost_by_model.items():
        ai_cost = float(values.get("ai_cost_cny") or 0.0)
        savings = float(values.get("replacement_savings_cny") or 0.0)
        roi_ratio = savings / ai_cost if ai_cost > 0 else None
        finalized[model] = {
            "currency": "CNY",
            "turns": values.get("turns", 0),
            "ai_cost_cny": round_money(ai_cost, 4),
            "input_cost_cny": round_money(float(values.get("input_cost_cny") or 0.0), 4),
            "uncached_input_cost_cny": round_money(float(values.get("uncached_input_cost_cny") or 0.0), 4),
            "cached_input_cost_cny": round_money(float(values.get("cached_input_cost_cny") or 0.0), 4),
            "output_cost_cny": round_money(float(values.get("output_cost_cny") or 0.0), 4),
            "traditional_cost_cny": round_money(float(values.get("traditional_cost_cny") or 0.0), 2),
            "replacement_savings_cny": round_money(savings, 4),
            "roi_ratio": round_money(roi_ratio, 4),
            "roi_percent": round_money(roi_ratio * 100 if roi_ratio is not None else None, 2),
        }
    return finalized


def build_value_report(root: Path, out_dir: Path, project_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = project_summary
    if summary is None:
        loaded = load_json(out_dir / "project-summary.json")
        summary = loaded if isinstance(loaded, dict) and loaded.get("schema_version") == PROJECT_SUMMARY_SCHEMA_VERSION else refresh_project_summary(root, out_dir)
    cost_totals = empty_cost_totals()
    cost_by_model: dict[str, dict[str, Any]] = {}
    task_value_history: list[dict[str, Any]] = []
    task_metrics = summary.get("task_metrics") if isinstance(summary.get("task_metrics"), dict) else {}

    task_history = summary.get("task_history") if isinstance(summary.get("task_history"), list) else []
    for item in task_history:
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        item_models = item.get("models") if isinstance(item.get("models"), dict) else {}
        model = str(item.get("model") or (next(iter(item_models.keys())) if len(item_models) == 1 else "multiple"))
        usage = normalize_usage(item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {})
        economics = estimate_turn_economics(
            model,
            usage,
            task.get("complexity"),
            task.get("complexity_reason"),
        )
        add_cost_totals(cost_totals, cost_by_model, model, economics)
        task_value_history.append(
            {
                "task_id": item.get("task_id"),
                "turn_index": item.get("turn_index"),
                "recorded_at": item.get("recorded_at") or item.get("updated_at"),
                "model": model,
                "task": task,
                "timing": item.get("timing"),
                "token_usage": usage or {key: 0 for key in USAGE_KEYS},
                "git": item.get("git"),
                "cost_estimate": {
                    "currency": "CNY",
                    "pricing_available": economics.get("pricing_available"),
                    "model_price_key": economics.get("model_price_key"),
                    "input_usd_per_1m": economics.get("input_usd_per_1m"),
                    "cached_input_usd_per_1m": economics.get("cached_input_usd_per_1m"),
                    "output_usd_per_1m": economics.get("output_usd_per_1m"),
                    "ai_cost_cny": economics.get("ai_cost_cny"),
                    "input_cost_cny": economics.get("input_cost_cny"),
                    "uncached_input_cost_cny": economics.get("uncached_input_cost_cny"),
                    "cached_input_cost_cny": economics.get("cached_input_cost_cny"),
                    "output_cost_cny": economics.get("output_cost_cny"),
                    "traditional_hours": economics.get("traditional_hours"),
                    "traditional_cost_cny": economics.get("traditional_cost_cny"),
                    "replacement_savings_cny": economics.get("replacement_savings_cny"),
                    "roi_ratio": economics.get("roi_ratio"),
                    "roi_percent": economics.get("roi_percent"),
                },
            }
        )

    return {
        "schema_version": 1,
        "project": summary.get("project") or root.name,
        "generated_at": utc_now(),
        "source_summary_file": ".agent/usage/project-summary.json",
        "source_summary_updated_at": summary.get("updated_at"),
        "recorded_tasks": task_metrics.get("recorded_tasks", len(task_history)),
        "cost_policy": cost_policy(),
        "cost_totals": finalize_cost_totals(cost_totals),
        "cost_by_model": finalize_cost_by_model(cost_by_model),
        "task_value_history": task_value_history,
        "notes": [
            "derived report generated from stable project-summary metadata and current pricing policy",
            "do not commit this report if pricing or unit assumptions should remain runtime-local",
        ],
    }


def newest_pending_path(out_dir: Path) -> Path | None:
    pending_dir = out_dir / "pending"
    try:
        candidates = [path for path in pending_dir.glob("*.json") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def set_current_turn_metadata(root: Path, out_dir: Path, argv: list[str]) -> int:
    description = None
    complexity = None
    reason = None
    for index, arg in enumerate(argv):
        if arg == "--description" and index + 1 < len(argv):
            description = argv[index + 1]
        elif arg == "--complexity" and index + 1 < len(argv):
            complexity = argv[index + 1]
        elif arg == "--reason" and index + 1 < len(argv):
            reason = argv[index + 1]

    pending_path = newest_pending_path(out_dir)
    if pending_path is None:
        print("no pending turn metadata target found", file=sys.stderr)
        return 1

    pending = load_json(pending_path) or {}
    pending["ai_task_metadata"] = {
        "description": sanitize_public_task_text(description) if description else "",
        "description_source": "ai_pending_metadata" if description else "ai_summary_missing",
        "complexity": normalize_complexity(complexity),
        "complexity_reason": str(reason) if reason else None,
        "complexity_source": "ai_pending_metadata" if complexity else "missing",
    }
    write_json(pending_path, pending)
    print(json.dumps({"ok": True, "pending_path": str(pending_path)}, ensure_ascii=False, sort_keys=True))
    return 0


def rebuild_summary(root: Path, out_dir: Path) -> dict[str, Any]:
    records_path = out_dir / "codex-turns.jsonl"
    records = read_records(records_path)
    token_totals = {key: 0 for key in USAGE_KEYS}
    prompt_estimate_total = 0
    assistant_estimate_total = 0
    elapsed_total = 0.0
    complexity_counts: dict[str, int] = {}
    by_model: dict[str, int] = {}
    task_history: list[dict[str, Any]] = []
    git_closed_loops = 0
    assisted_tasks = 0

    for index, record in enumerate(records, 1):
        turn_usage = ((record.get("token_usage") or {}).get("turn") or {})
        for key in USAGE_KEYS:
            token_totals[key] += int(turn_usage.get(key) or 0)

        prompt_estimate_total += int(((record.get("user_input") or {}).get("token_estimate") or 0))
        assistant_estimate_total += int(((record.get("assistant_output") or {}).get("token_estimate") or 0))
        elapsed_total += float(((record.get("timing") or {}).get("elapsed_seconds") or 0))

        model = record.get("model") or "unknown"
        by_model[model] = by_model.get(model, 0) + 1
        history_item = public_history_item(index, record)
        level = history_item["task"]["complexity"] or "unknown"
        complexity_counts[level] = complexity_counts.get(level, 0) + 1
        task_history.append(history_item)

        if ((record.get("git") or {}).get("git_closed_loop")):
            git_closed_loops += 1
        if ((record.get("assistant_output") or {}).get("text")):
            assisted_tasks += 1

    summary = {
        "schema_version": 2,
        "project_root": str(root),
        "records_file": str(records_path),
        "updated_at": utc_now(),
        "recorded_turns": len(records),
        "assisted_tasks_estimate": assisted_tasks,
        "git_closed_loops": git_closed_loops,
        "token_totals": token_totals,
        "user_prompt_token_estimate_total": prompt_estimate_total,
        "assistant_output_token_estimate_total": assistant_estimate_total,
        "elapsed_seconds_total": round(elapsed_total, 3),
        "complexity_counts": complexity_counts,
        "turns_by_model": by_model,
        "task_history": task_history,
        "last_recorded_at": records[-1].get("recorded_at") if records else None,
        "notes": [
            "turn token totals use Codex transcript cumulative deltas when available",
            "user prompt and assistant output token estimates are local text estimates",
            "project summary stores stable usage metadata only; derived value reports are regenerated separately",
            "usage records are project-local runtime data and should not be committed",
        ],
    }
    write_json(out_dir / "summary.json", summary)
    return summary


def refresh_project_summary(root: Path, out_dir: Path) -> dict[str, Any]:
    summary = rebuild_summary(root, out_dir)
    project_summary = load_project_summary(root, out_dir)
    return write_project_summary(root, out_dir, project_summary, summary)


def finalize_latest_project_task_from_git(root: Path, out_dir: Path) -> dict[str, Any]:
    commit = latest_git_commit(root)
    if not commit:
        return {"changed": False, "reason": "missing_git_commit"}

    project_summary = load_project_summary(root, out_dir)
    tasks = project_summary.get("task_history") if isinstance(project_summary.get("task_history"), list) else []
    candidates: list[tuple[dt.datetime, int, dict[str, Any]]] = []
    for index, item in enumerate(tasks):
        if not isinstance(item, dict):
            continue
        git = item.get("git") if isinstance(item.get("git"), dict) else {}
        if git.get("commit_sha"):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        turns = item.get("turns") if isinstance(item.get("turns"), dict) else {}
        sort_time = (
            parse_time(str(turns.get("last_recorded_at") or ""))
            or parse_time(str(item.get("updated_at") or ""))
            or parse_time(str(item.get("created_at") or ""))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        )
        status_bonus = 1 if normalize_project_task_status(task.get("status")) == "in_progress" else 0
        candidates.append((sort_time + dt.timedelta(microseconds=status_bonus), index, item))

    if not candidates:
        return {"changed": False, "reason": "no_unclosed_task"}

    _sort_time, index, item = max(candidates, key=lambda entry: (entry[0], entry[1]))
    task = dict(item.get("task") if isinstance(item.get("task"), dict) else {})
    task["status"] = "completed"
    updated = dict(item)
    updated["task"] = task
    updated["updated_at"] = utc_now()
    updated["git"] = {
        "git_closed_loop": True,
        "commit_sha": commit.get("sha"),
        "commit_subject": commit.get("subject"),
        "committed_at": commit.get("committed_at"),
    }
    tasks[index] = updated
    project_summary["task_history"] = tasks
    finalized = write_project_summary(root, out_dir, project_summary)
    return {
        "changed": True,
        "task_id": updated.get("task_id"),
        "commit_sha": commit.get("sha"),
        "recorded_tasks": finalized.get("task_metrics", {}).get("recorded_tasks"),
    }


def append_turn_record(root: Path, out_dir: Path, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    records_path = out_dir / "codex-turns.jsonl"
    lock_path = out_dir / ".lock"
    with UsageLock(lock_path):
        existing_ids = {item.get("record_id") for item in read_records(records_path)}
        appended = False
        if record.get("record_id") not in existing_ids:
            with records_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            appended = True
        return rebuild_summary(root, out_dir), appended


def record_prompt(event: dict[str, Any], root: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pending_dir = out_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(event.get("session_id") or "unknown")
    turn_id = str(event.get("turn_id") or "unknown")
    model = event.get("model")
    prompt = event.get("prompt") or ""
    transcript_path = event.get("transcript_path")
    token_snapshot = latest_token_count(transcript_path)
    token_estimate = estimate_text_tokens(prompt, model)
    start_git = {
        "head": run_text(["git", "rev-parse", "--short", "HEAD"], root),
        "status_lines": git_status_lines(root),
    }
    pending = {
        "schema_version": 2,
        "session_id": session_id,
        "turn_id": turn_id,
        "model": model,
        "cwd": event.get("cwd"),
        "transcript_path": transcript_path,
        "started_at": utc_now(),
        "prompt": prompt,
        "prompt_char_count": len(prompt),
        "prompt_line_count": line_count(prompt),
        "prompt_token_estimate": token_estimate["tokens"],
        "prompt_token_estimate_method": token_estimate["method"],
        "start_git": start_git,
        "start_token_snapshot": token_snapshot,
        "start_total_token_usage": (token_snapshot or {}).get("total_token_usage"),
    }
    pending_path = pending_dir / f"{safe_name(session_id)}__{safe_name(turn_id)}.json"
    write_json(pending_path, pending)


def record_stop(event: dict[str, Any], root: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    session_id = str(event.get("session_id") or "unknown")
    turn_id = str(event.get("turn_id") or "unknown")
    pending_path = out_dir / "pending" / f"{safe_name(session_id)}__{safe_name(turn_id)}.json"
    pending = load_json(pending_path) or {}

    transcript_path = event.get("transcript_path") or pending.get("transcript_path")
    end_snapshot = latest_token_count_with_retry(transcript_path)
    start_usage = normalize_usage(pending.get("start_total_token_usage"))
    end_usage = normalize_usage((end_snapshot or {}).get("total_token_usage"))
    delta_usage = subtract_usage(end_usage, start_usage)
    last_usage = normalize_usage((end_snapshot or {}).get("last_token_usage"))
    turn_usage = delta_usage or last_usage or {key: 0 for key in USAGE_KEYS}
    usage_source = "transcript_total_delta" if delta_usage else "transcript_last_token_usage"

    started_at = pending.get("started_at")
    ended_at = utc_now()
    start_time = parse_time(started_at)
    end_time = parse_time(ended_at)
    elapsed_seconds = None
    if start_time and end_time:
        elapsed_seconds = max(0.0, (end_time - start_time).total_seconds())

    prompt = pending.get("prompt")
    assistant_message = event.get("last_assistant_message") or ""
    assistant_estimate = estimate_text_tokens(assistant_message, event.get("model") or pending.get("model"))
    git_info = git_snapshot(root, started_at, pending.get("start_git"))
    ai_task = turn_ai_task_metadata(event, pending, assistant_message)

    record = {
        "schema_version": 2,
        "record_id": f"{session_id}:{turn_id}",
        "recorded_at": ended_at,
        "project_root": str(root),
        "cwd": event.get("cwd") or pending.get("cwd"),
        "session_id": session_id,
        "turn_id": turn_id,
        "model": event.get("model") or pending.get("model"),
        "task": {
            "description": ai_task["description"],
            "description_source": ai_task["description_source"],
            "complexity": ai_task["complexity"],
            "complexity_reason": ai_task["complexity_reason"],
            "complexity_source": ai_task["complexity_source"],
        },
        "timing": {
            "started_at": started_at,
            "ended_at": ended_at,
            "elapsed_seconds": round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
        },
        "user_input": {
            "text": prompt,
            "char_count": int(pending.get("prompt_char_count") or len(prompt or "")),
            "line_count": int(pending.get("prompt_line_count") or line_count(prompt)),
            "token_estimate": int(pending.get("prompt_token_estimate") or 0),
            "token_estimate_method": pending.get("prompt_token_estimate_method"),
        },
        "assistant_output": {
            "text": assistant_message,
            "char_count": len(assistant_message),
            "line_count": line_count(assistant_message),
            "token_estimate": assistant_estimate["tokens"],
            "token_estimate_method": assistant_estimate["method"],
        },
        "token_usage": {
            "source": usage_source,
            "turn": turn_usage,
            "start_total": start_usage,
            "end_total": end_usage,
            "last_model_call": last_usage,
            "model_context_window": (end_snapshot or {}).get("model_context_window"),
            "transcript_token_line": (end_snapshot or {}).get("line_number"),
        },
        "git": git_info,
        "hook": {
            "hook_event_name": event.get("hook_event_name"),
            "stop_hook_active": event.get("stop_hook_active"),
            "transcript_path": transcript_path,
            "pending_path": str(pending_path),
        },
    }
    summary, appended = append_turn_record(root, out_dir, record)
    if not appended:
        try:
            pending_path.unlink()
        except OSError:
            pass
        return
    project_task_update = pending.get("project_task_update")
    if isinstance(project_task_update, dict):
        apply_project_task_update(root, out_dir, project_task_update, current_record_id=record["record_id"])
    maybe_start_project_summary_agent(root, out_dir, record["record_id"])
    try:
        pending_path.unlink()
    except OSError:
        pass


def log_error(out_dir: Path, message: str) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        with (out_dir / "hook-errors.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": utc_now(), "error": message}, sort_keys=True) + "\n")
    except OSError:
        pass


def hook_success() -> None:
    print(json.dumps({"continue": True, "suppressOutput": True}, sort_keys=True))


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--rebuild-summary":
        root = resolve_repo_root(os.getcwd())
        rebuild_summary(root, usage_dir(root))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh-project-summary":
        root = resolve_repo_root(os.getcwd())
        refresh_project_summary(root, usage_dir(root))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--finalize-latest-project-task-from-git":
        root = resolve_repo_root(os.getcwd())
        result = finalize_latest_project_task_from_git(root, usage_dir(root))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--write-value-report":
        root = resolve_repo_root(os.getcwd())
        out_dir = usage_dir(root)
        output = Path(sys.argv[2]) if len(sys.argv) > 2 else out_dir / "value-report.json"
        if not output.is_absolute():
            output = root / output
        report = build_value_report(root, out_dir)
        write_json(output, report)
        print(str(output))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--print-value-report":
        root = resolve_repo_root(os.getcwd())
        out_dir = usage_dir(root)
        report = build_value_report(root, out_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--set-current-turn-metadata":
        root = resolve_repo_root(os.getcwd())
        return set_current_turn_metadata(root, usage_dir(root), sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--record-project-task":
        root = resolve_repo_root(os.getcwd())
        return record_project_task(root, usage_dir(root), sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--run-project-summary-agent":
        root = resolve_repo_root(os.getcwd())
        record_id = None
        for index, arg in enumerate(sys.argv[2:]):
            if arg == "--record-id" and index + 3 < len(sys.argv):
                record_id = sys.argv[index + 3]
        return run_project_summary_agent(root, usage_dir(root), record_id)

    raw = sys.stdin.read()
    try:
        event = json.loads(raw or "{}")
    except json.JSONDecodeError:
        event = {}

    root = resolve_repo_root(event.get("cwd"))
    out_dir = usage_dir(root)
    try:
        hook_event = event.get("hook_event_name")
        if hook_event == "UserPromptSubmit":
            record_prompt(event, root, out_dir)
        elif hook_event == "Stop":
            record_stop(event, root, out_dir)
    except Exception as exc:  # Hooks should never block normal Codex work.
        log_error(out_dir, f"{type(exc).__name__}: {exc}")

    hook_success()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
