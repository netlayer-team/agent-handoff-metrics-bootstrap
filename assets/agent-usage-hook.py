#!/usr/bin/env python3
"""Record Codex turn usage into the project-local .agent directory.

This script is designed to be called by Codex hooks. It records the prompt at
UserPromptSubmit and completes the turn record at Stop by reading Codex's
transcript token_count events.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
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
    override = os.environ.get("CLRSP_AGENT_USAGE_DIR")
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
        level: env_float(f"CLRSP_AGENT_TRADITIONAL_HOURS_{level.upper()}", default, minimum=0.0)
        for level, default in DEFAULT_TRADITIONAL_HOURS.items()
    }
    return {
        "currency": "CNY",
        "usd_to_cny": env_float("CLRSP_AGENT_USD_TO_CNY", DEFAULT_USD_TO_CNY, minimum=0.000001),
        "usd_to_cny_source": "CLRSP_AGENT_USD_TO_CNY or project default",
        "engineer_hourly_rate_cny": env_float(
            "CLRSP_AGENT_ENGINEER_HOURLY_RATE_CNY",
            DEFAULT_ENGINEER_HOURLY_RATE_CNY,
            minimum=0.0,
        ),
        "traditional_hours_by_complexity": hours,
        "model_price_source": "OpenAI API pricing, standard processing, USD per 1M tokens",
        "model_price_overrides": "edit MODEL_PRICES_USD_PER_1M or set project-specific hook policy",
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
    return normalized if normalized in {"low", "medium", "high", "unknown"} else "unknown"


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


def empty_cost_totals() -> dict[str, Any]:
    return {
        "currency": "CNY",
        "priced_turns": 0,
        "unpriced_turns": 0,
        "ai_cost_usd": 0.0,
        "ai_cost_cny": 0.0,
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
    totals["traditional_cost_cny_priced"] += traditional
    totals["replacement_savings_cny"] += float(economics.get("replacement_savings_cny") or 0.0)

    model_entry = cost_by_model.setdefault(
        model,
        {
            "currency": "CNY",
            "turns": 0,
            "ai_cost_cny": 0.0,
            "traditional_cost_cny": 0.0,
            "replacement_savings_cny": 0.0,
        },
    )
    model_entry["turns"] += 1
    model_entry["ai_cost_cny"] += float(ai_cost_cny)
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
            "traditional_cost_cny": round_money(float(values.get("traditional_cost_cny") or 0.0), 2),
            "replacement_savings_cny": round_money(savings, 4),
            "roi_ratio": round_money(roi_ratio, 4),
            "roi_percent": round_money(roi_ratio * 100 if roi_ratio is not None else None, 2),
        }
    return finalized


def build_value_report(root: Path, out_dir: Path, project_summary: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = project_summary or load_json(out_dir / "project-summary.json") or rebuild_summary(root, out_dir)
    cost_totals = empty_cost_totals()
    cost_by_model: dict[str, dict[str, Any]] = {}
    task_value_history: list[dict[str, Any]] = []

    task_history = summary.get("task_history") if isinstance(summary.get("task_history"), list) else []
    for item in task_history:
        if not isinstance(item, dict):
            continue
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        model = str(item.get("model") or "unknown")
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
                "turn_index": item.get("turn_index"),
                "recorded_at": item.get("recorded_at"),
                "model": model,
                "task": task,
                "timing": item.get("timing"),
                "token_usage": usage or {key: 0 for key in USAGE_KEYS},
                "git": item.get("git"),
                "cost_estimate": {
                    "currency": "CNY",
                    "pricing_available": economics.get("pricing_available"),
                    "model_price_key": economics.get("model_price_key"),
                    "ai_cost_cny": economics.get("ai_cost_cny"),
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
        "recorded_turns": summary.get("recorded_turns", 0),
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
    write_public_summary(root, out_dir, summary)
    return summary


def write_public_summary(root: Path, out_dir: Path, summary: dict[str, Any]) -> None:
    public_summary = {
        "schema_version": 2,
        "project": root.name,
        "updated_at": summary.get("updated_at"),
        "recorded_turns": summary.get("recorded_turns", 0),
        "assisted_tasks_estimate": summary.get("assisted_tasks_estimate", 0),
        "git_closed_loops": summary.get("git_closed_loops", 0),
        "token_totals": summary.get("token_totals", {}),
        "user_prompt_token_estimate_total": summary.get("user_prompt_token_estimate_total", 0),
        "assistant_output_token_estimate_total": summary.get("assistant_output_token_estimate_total", 0),
        "elapsed_seconds_total": summary.get("elapsed_seconds_total", 0),
        "complexity_counts": summary.get("complexity_counts", {}),
        "turns_by_model": summary.get("turns_by_model", {}),
        "task_history": summary.get("task_history", []),
        "last_recorded_at": summary.get("last_recorded_at"),
        "privacy": {
            "contains_user_prompts": False,
            "contains_assistant_outputs": False,
            "contains_task_descriptions": True,
            "task_descriptions_are_ai_summarized": True,
            "task_descriptions_may_be_result_or_commit_derived": True,
            "contains_session_ids": False,
            "contains_transcript_paths": False,
            "contains_derived_costs": False,
            "contains_roi": False,
            "source_detail_file": ".agent/usage/codex-turns.jsonl",
            "source_detail_file_committed": False,
        },
        "notes": [
            "sanitized project usage metadata intended for Git tracking",
            "task_history keeps AI task summaries and stable metrics, not full prompts or assistant output",
            "cost and ROI are regenerated into a separate value report from this summary and current policy",
            "raw per-turn records stay local because they contain prompts, assistant output, and local paths",
        ],
    }
    write_json(out_dir / "project-summary.json", public_summary)


def append_turn_record(root: Path, out_dir: Path, record: dict[str, Any]) -> None:
    records_path = out_dir / "codex-turns.jsonl"
    lock_path = out_dir / ".lock"
    with UsageLock(lock_path):
        existing_ids = {item.get("record_id") for item in read_records(records_path)}
        if record.get("record_id") not in existing_ids:
            with records_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        rebuild_summary(root, out_dir)


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
    append_turn_record(root, out_dir, record)
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
