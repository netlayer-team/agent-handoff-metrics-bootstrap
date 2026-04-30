#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def infer_project_root(usage_dir: Path) -> Path:
    resolved = usage_dir.resolve()
    if resolved.name == "usage" and resolved.parent.name == ".agent":
        return resolved.parent.parent
    return project_root_from_script()


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object in {path}")
    return data


def maybe_read_json(path: Path) -> dict[str, Any]:
    return read_json(path) if path.exists() else {}


def read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        return records
    return records


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"¥{float(value):,.{digits}f}"


def number(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.{digits}f}"
    return f"{int(value):,}"


def pct(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{digits}f}%"


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, int]:
    raw = raw if isinstance(raw, dict) else {}
    usage = {key: int(raw.get(key) or 0) for key in USAGE_KEYS if key != "uncached_input_tokens"}
    usage["uncached_input_tokens"] = max(0, usage["input_tokens"] - usage["cached_input_tokens"])
    return {key: int(usage.get(key) or 0) for key in USAGE_KEYS}


def empty_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


def add_usage(target: dict[str, int], usage: dict[str, Any] | None) -> None:
    normalized = normalize_usage(usage)
    for key in USAGE_KEYS:
        target[key] = int(target.get(key) or 0) + int(normalized.get(key) or 0)


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def fmt_time(value: Any) -> str:
    parsed = parse_time(value)
    if not parsed:
        return "-"
    return parsed.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M 北京时间")


def fmt_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def period_usage_from_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, dict[str, dict[str, Any]]] = {"daily": {}, "weekly": {}, "monthly": {}}

    def ensure(kind: str, key: str, label: str) -> dict[str, Any]:
        return buckets[kind].setdefault(
            key,
            {
                "period": key,
                "label": label,
                "turns": 0,
                "token_usage": empty_usage(),
            },
        )

    for record in records:
        parsed = parse_time(record.get("recorded_at"))
        if not parsed:
            continue
        local_date = parsed.astimezone(CHINA_TZ).date()
        usage = ((record.get("token_usage") or {}).get("turn") or {})
        if not isinstance(usage, dict):
            usage = {}
        iso = local_date.isocalendar()
        specs = (
            ("daily", local_date.isoformat(), local_date.strftime("%m-%d")),
            ("weekly", f"{iso.year}-W{iso.week:02d}", f"{iso.year} W{iso.week:02d}"),
            ("monthly", local_date.strftime("%Y-%m"), local_date.strftime("%Y-%m")),
        )
        for kind, key, label in specs:
            bucket = ensure(kind, key, label)
            bucket["turns"] = int(bucket.get("turns") or 0) + 1
            add_usage(bucket["token_usage"], usage)

    return {
        kind: sorted(items.values(), key=lambda item: str(item.get("period") or ""))
        for kind, items in buckets.items()
    }


def css_width(value: float | int | None, max_value: float | int | None, floor: float = 2.0) -> str:
    if value is None or not max_value:
        return "0%"
    ratio = max(0.0, min(100.0, float(value) / float(max_value) * 100.0))
    if ratio > 0:
        ratio = max(floor, ratio)
    return f"{ratio:.2f}%"


def complexity_label(value: Any) -> str:
    return {
        "low": "低",
        "medium": "中",
        "high": "高",
        "unknown": "未知",
    }.get(str(value or ""), str(value or "-"))


def category_label(value: Any) -> str:
    return {
        "implementation": "实现",
        "debugging": "调试",
        "design": "设计",
        "review": "评审",
        "ops": "运维",
        "documentation": "文档",
        "maintenance": "维护",
        "other": "其它",
    }.get(str(value or ""), str(value or "-"))


def status_label(value: Any) -> str:
    return {
        "completed": "已完成",
        "in_progress": "进行中",
        "planned": "计划中",
        "blocked": "阻塞",
    }.get(str(value or ""), str(value or "-"))


def task_title(item: dict[str, Any]) -> str:
    task = item.get("task") if isinstance(item.get("task"), dict) else {}
    return str(task.get("title") or task.get("description") or item.get("task_id") or "未命名任务")


def infer_model(item: dict[str, Any]) -> str:
    if item.get("model"):
        return str(item["model"])
    models = item.get("models")
    if isinstance(models, dict) and len(models) == 1:
        return str(next(iter(models.keys())))
    if isinstance(models, dict) and models:
        return "multiple"
    return "unknown"


def load_value_report(
    usage_dir: Path,
    project_root: Path,
    project_summary: dict[str, Any],
    skip_project_hook: bool,
) -> tuple[dict[str, Any], str]:
    hook = project_root / ".agent" / "scripts" / "agent-usage-hook.py"
    if not skip_project_hook and hook.exists():
        env = os.environ.copy()
        env["AGENT_USAGE_DIR"] = str(usage_dir)
        try:
            proc = subprocess.run(
                [sys.executable, str(hook), "--print-value-report"],
                cwd=str(project_root),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=45,
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                if isinstance(data, dict) and isinstance(data.get("task_value_history"), list):
                    return data, "project hook --print-value-report"
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    existing_path = usage_dir / "value-report.json"
    existing = maybe_read_json(existing_path)
    if isinstance(existing.get("task_value_history"), list):
        if existing.get("source_summary_updated_at") == project_summary.get("updated_at"):
            return existing, "existing value-report.json"
        return existing, "existing value-report.json, source timestamp differs"

    raise SystemExit(
        "cannot build value report: project hook failed and .agent/usage/value-report.json is unavailable"
    )


def task_history_index(project_summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    task_history = project_summary.get("task_history") if isinstance(project_summary.get("task_history"), list) else []
    indexed: dict[str, dict[str, Any]] = {}
    for item in task_history:
        if isinstance(item, dict) and item.get("task_id"):
            indexed[str(item["task_id"])] = item
    return indexed


def merge_task_value_history(project_summary: dict[str, Any], value_report: dict[str, Any]) -> dict[str, Any]:
    source_by_id = task_history_index(project_summary)
    report = dict(value_report)
    history: list[dict[str, Any]] = []
    for raw in value_report.get("task_value_history", []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        source = source_by_id.get(str(item.get("task_id") or ""))
        if source:
            for key in ("turns", "models", "created_at", "updated_at"):
                if key not in item and source.get(key) is not None:
                    item[key] = source.get(key)
            for key in ("git", "timing", "token_usage"):
                if not isinstance(item.get(key), dict) and isinstance(source.get(key), dict):
                    item[key] = source.get(key)
            if not item.get("recorded_at"):
                item["recorded_at"] = source.get("updated_at") or source.get("created_at")
            source_task = source.get("task") if isinstance(source.get("task"), dict) else {}
            task = dict(item.get("task") if isinstance(item.get("task"), dict) else {})
            for key, value in source_task.items():
                task.setdefault(key, value)
            item["task"] = task
        item["token_usage"] = normalize_usage(item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {})
        history.append(item)
    report["task_value_history"] = history
    return report


def sorted_tasks(value_report: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = [item for item in value_report.get("task_value_history", []) if isinstance(item, dict)]
    def sort_time(item: dict[str, Any]) -> dt.datetime:
        turns = item.get("turns") if isinstance(item.get("turns"), dict) else {}
        return (
            parse_time(turns.get("last_recorded_at"))
            or parse_time(item.get("updated_at"))
            or parse_time(item.get("recorded_at"))
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        )

    return sorted(
        tasks,
        key=sort_time,
        reverse=True,
    )


def task_turn_count(item: dict[str, Any]) -> int:
    turns = item.get("turns") if isinstance(item.get("turns"), dict) else {}
    indices = turns.get("turn_indices") if isinstance(turns.get("turn_indices"), list) else []
    try:
        count = int(turns.get("turn_count") or 0)
    except (TypeError, ValueError):
        count = 0
    return count or len(indices) or 1


def turn_indices_note(item: dict[str, Any]) -> str:
    turns = item.get("turns") if isinstance(item.get("turns"), dict) else {}
    indices = turns.get("turn_indices") if isinstance(turns.get("turn_indices"), list) else []
    if not indices:
        return ""
    return "turn_indices: " + ", ".join(str(index) for index in indices)


def git_label(git: dict[str, Any]) -> str:
    commit_sha = str(git.get("commit_sha") or "")
    if commit_sha:
        return commit_sha[:7]
    if git.get("git_closed_loop"):
        return "已闭环，无提交号"
    return "未闭环"


def git_label_title(git: dict[str, Any]) -> str:
    subject = str(git.get("commit_subject") or "")
    committed_at = str(git.get("committed_at") or "")
    commit_sha = str(git.get("commit_sha") or "")
    if commit_sha:
        return " ".join(part for part in (commit_sha, subject, committed_at) if part)
    if git.get("git_closed_loop"):
        return "git_closed_loop=true，但当前任务摘要没有记录 commit_sha"
    return "未记录 Git 提交闭环"


def render_metric(label: str, value: str, note: str = "", value_id: str = "", note_id: str = "") -> str:
    value_attr = f' id="{escape(value_id)}"' if value_id else ""
    note_attr = f' id="{escape(note_id)}"' if note_id else ""
    return f"""
    <section class="metric">
      <span>{escape(label)}</span>
      <strong{value_attr}>{escape(value)}</strong>
      <small{note_attr}>{escape(note)}</small>
    </section>
    """


def render_distribution(title: str, counts: dict[str, Any], labels: dict[str, str] | None = None) -> str:
    labels = labels or {}
    entries = [(key, int(value or 0)) for key, value in counts.items()]
    total = sum(value for _, value in entries) or 1
    rows = []
    for key, value in sorted(entries, key=lambda item: (-item[1], item[0])):
        rows.append(
            f"""
            <div class="dist-row">
              <span>{escape(labels.get(key, key))}</span>
              <div class="dist-track"><i style="width: {value / total * 100:.2f}%"></i></div>
              <b>{number(value)}</b>
            </div>
            """
        )
    return f"""
    <section class="panel">
      <div class="panel-head">
        <h2>{escape(title)}</h2>
      </div>
      <div class="dist-list">{''.join(rows)}</div>
    </section>
    """


def render_cost_compare(cost_totals: dict[str, Any]) -> str:
    ai = float(cost_totals.get("ai_cost_cny") or 0.0)
    traditional = float(cost_totals.get("traditional_cost_cny_all") or 0.0)
    max_value = max(ai, traditional, 1.0)
    return f"""
    <section class="panel cost-panel">
      <div class="panel-head">
        <h2>成本替代</h2>
        <p>按当前工程策略估算</p>
      </div>
      <div class="cost-bars">
        <div class="cost-row">
          <span>AI 成本</span>
          <div class="bar-track ai"><i id="ai-cost-bar" style="width: {css_width(ai, max_value)}"></i></div>
          <strong id="cost-panel-ai">{money(ai)}</strong>
        </div>
        <div class="cost-row">
          <span>传统成本</span>
          <div class="bar-track traditional"><i id="traditional-cost-bar" style="width: {css_width(traditional, max_value)}"></i></div>
          <strong id="cost-panel-traditional">{money(traditional)}</strong>
        </div>
      </div>
      <div class="saving-line">
        <span>节约</span>
        <strong id="cost-panel-savings">{money(cost_totals.get("replacement_savings_cny"))}</strong>
      </div>
      <div class="cost-breakdown">
        <div><span>输入成本</span><strong id="cost-panel-input">{money(cost_totals.get("input_cost_cny"))}</strong></div>
        <div><span>输出成本</span><strong id="cost-panel-output">{money(cost_totals.get("output_cost_cny"))}</strong></div>
        <div><span>缓存输入</span><strong id="cost-panel-cached">{money(cost_totals.get("cached_input_cost_cny"))}</strong></div>
        <div><span>非缓存输入</span><strong id="cost-panel-uncached">{money(cost_totals.get("uncached_input_cost_cny"))}</strong></div>
      </div>
    </section>
    """


def render_token_stack(tokens: dict[str, Any]) -> str:
    cached = int(tokens.get("cached_input_tokens") or 0)
    uncached = int(tokens.get("uncached_input_tokens") or 0)
    output = int(tokens.get("output_tokens") or 0)
    total = max(cached + uncached + output, 1)
    return f"""
    <section class="panel token-panel">
      <div class="panel-head">
        <h2>累计 Token 构成</h2>
        <p>{number(tokens.get("total_tokens"))} total</p>
      </div>
      <div class="token-stack" aria-label="token composition">
        <i class="cached" style="width: {cached / total * 100:.2f}%"></i>
        <i class="uncached" style="width: {uncached / total * 100:.2f}%"></i>
        <i class="output" style="width: {output / total * 100:.2f}%"></i>
      </div>
      <div class="legend">
        <span><i class="dot cached"></i>缓存输入 {number(cached)}</span>
        <span><i class="dot uncached"></i>非缓存输入 {number(uncached)}</span>
        <span><i class="dot output"></i>输出 {number(output)}</span>
      </div>
    </section>
    """


def render_model_table(cost_by_model: dict[str, Any]) -> str:
    rows = []
    for model, item in sorted(cost_by_model.items()):
        if not isinstance(item, dict):
            continue
        rows.append(
            f"""
            <tr>
              <td>{escape(model)}</td>
              <td>{number(item.get("turns"))}</td>
              <td>{money(item.get("ai_cost_cny"))}</td>
              <td>{money(item.get("input_cost_cny"))}</td>
              <td>{money(item.get("output_cost_cny"))}</td>
              <td>{money(item.get("traditional_cost_cny"))}</td>
              <td>{money(item.get("replacement_savings_cny"))}</td>
              <td>{pct(item.get("roi_percent"))}</td>
            </tr>
            """
        )
    return f"""
    <section class="panel table-panel">
      <div class="panel-head">
        <h2>模型成本</h2>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>模型</th>
              <th>任务</th>
              <th>AI 成本</th>
              <th>输入成本</th>
              <th>输出成本</th>
              <th>传统成本</th>
              <th>节约</th>
              <th>ROI</th>
            </tr>
          </thead>
          <tbody id="model-cost-body">{''.join(rows)}</tbody>
        </table>
      </div>
    </section>
    """


def model_prices_for_tasks(policy: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    configured = policy.get("model_prices_usd_per_1m") if isinstance(policy.get("model_prices_usd_per_1m"), dict) else {}
    prices: dict[str, dict[str, float | None]] = {
        str(model): {
            "input": price.get("input"),
            "cached_input": price.get("cached_input"),
            "output": price.get("output"),
        }
        for model, price in configured.items()
        if isinstance(price, dict)
    }
    for item in tasks:
        cost = item.get("cost_estimate") if isinstance(item.get("cost_estimate"), dict) else {}
        key = str(cost.get("model_price_key") or infer_model(item) or "unknown")
        if not key or key == "None":
            continue
        if key in prices:
            continue
        configured_price = configured.get(key) if isinstance(configured.get(key), dict) else {}
        prices[key] = {
            "input": configured_price.get("input", cost.get("input_usd_per_1m")),
            "cached_input": configured_price.get("cached_input", cost.get("cached_input_usd_per_1m")),
            "output": configured_price.get("output", cost.get("output_usd_per_1m")),
        }
    return prices


def render_cost_controls(policy: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    hours = policy.get("traditional_hours_by_complexity") if isinstance(policy.get("traditional_hours_by_complexity"), dict) else {}
    prices = model_prices_for_tasks(policy, tasks)
    used_models: list[str] = []
    for item in tasks:
        cost = item.get("cost_estimate") if isinstance(item.get("cost_estimate"), dict) else {}
        model = str(cost.get("model_price_key") or infer_model(item) or "")
        if model and model in prices and model not in used_models:
            used_models.append(model)
    ordered_models = used_models + [model for model in sorted(prices) if model not in used_models]
    options = []
    for index, model in enumerate(ordered_models):
        selected = " selected" if index == 0 else ""
        options.append(f'<option value="{escape(model)}"{selected}>{escape(model)}</option>')
    return f"""
    <section class="panel control-panel">
      <div class="panel-head">
        <h2>成本参数</h2>
        <p>修改后即时重算</p>
      </div>
      <div class="control-grid">
        <label>工程师 ¥/h<input id="rate-input" type="number" min="0" step="10" value="{escape(policy.get("engineer_hourly_rate_cny") or 300)}"></label>
        <label>低复杂度 h<input id="hours-low-input" type="number" min="0" step="0.25" value="{escape(hours.get("low") or 0.5)}"></label>
        <label>中复杂度 h<input id="hours-medium-input" type="number" min="0" step="0.25" value="{escape(hours.get("medium") or 2)}"></label>
        <label>高复杂度 h<input id="hours-high-input" type="number" min="0" step="0.25" value="{escape(hours.get("high") or 8)}"></label>
        <label>未知复杂度 h<input id="hours-unknown-input" type="number" min="0" step="0.25" value="{escape(hours.get("unknown") or 1)}"></label>
        <label>USD/CNY<input id="usd-cny-input" type="number" min="0" step="0.05" value="{escape(policy.get("usd_to_cny") or 7.2)}"></label>
      </div>
      <div class="price-controls">
        <h3>模型价格</h3>
        <div class="price-editor">
          <label><span>模型</span><select id="model-price-select">{''.join(options)}</select></label>
          <label><span>输入 USD/1M</span><input id="model-price-input" type="number" min="0" step="0.001"></label>
          <label><span>缓存 USD/1M</span><input id="model-price-cached" type="number" min="0" step="0.001"></label>
          <label><span>输出 USD/1M</span><input id="model-price-output" type="number" min="0" step="0.001"></label>
        </div>
      </div>
    </section>
    """


def render_period_usage_panel() -> str:
    return """
    <section class="panel usage-panel">
      <div class="panel-head">
        <h2>Token 趋势</h2>
        <div class="segmented" role="group" aria-label="token usage period">
          <button type="button" data-period="daily" class="active">日</button>
          <button type="button" data-period="weekly">周</button>
          <button type="button" data-period="monthly">月</button>
        </div>
      </div>
      <div class="usage-list" id="period-usage-list"></div>
    </section>
    """


def json_for_script(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")


def interactive_script() -> str:
    return r"""
<script>
(() => {
  const dataNode = document.getElementById("report-data");
  const data = dataNode ? JSON.parse(dataNode.textContent || "{}") : {};
  const basePolicy = data.costPolicy || {};
  const baseUsdToCny = Number(basePolicy.usd_to_cny || 7.2) || 7.2;
  const taskRows = Array.from(document.querySelectorAll(".task-row"));
  const modelPrices = new Map(Object.entries(data.modelPrices || {}).map(([key, value]) => [key, {
    input: Number(value?.input || 0) || 0,
    cached: Number(value?.cached_input || value?.cached || 0) || 0,
    output: Number(value?.output || 0) || 0,
  }]));
  const state = { page: 1, pageSize: 5, period: "daily" };

  const money = (value) => `¥${Number(value || 0).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const pct = (value) => value == null || !Number.isFinite(value) ? "-" : `${Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
  const number = (value, digits = 0) => Number(value || 0).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const setText = (id, value) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  };
  const setWidth = (id, value, maxValue) => {
    const node = document.getElementById(id);
    if (!node) return;
    const max = Number(maxValue || 0);
    const width = max > 0 ? Math.max(0, Math.min(100, Number(value || 0) / max * 100)) : 0;
    node.style.width = `${width.toFixed(2)}%`;
  };

  const readPolicy = () => ({
    hourly: Number(document.getElementById("rate-input")?.value || basePolicy.engineer_hourly_rate_cny || 300) || 0,
    usdToCny: Number(document.getElementById("usd-cny-input")?.value || baseUsdToCny) || baseUsdToCny,
    hours: {
      low: Number(document.getElementById("hours-low-input")?.value || 0) || 0,
      medium: Number(document.getElementById("hours-medium-input")?.value || 0) || 0,
      high: Number(document.getElementById("hours-high-input")?.value || 0) || 0,
      unknown: Number(document.getElementById("hours-unknown-input")?.value || 0) || 0,
    },
  });

  function readModelPrices() {
    return modelPrices;
  }

  function selectedPriceKey() {
    return document.getElementById("model-price-select")?.value || "";
  }

  function syncPriceInputsFromState() {
    const price = modelPrices.get(selectedPriceKey()) || { input: 0, cached: 0, output: 0 };
    const input = document.getElementById("model-price-input");
    const cached = document.getElementById("model-price-cached");
    const output = document.getElementById("model-price-output");
    if (input) input.value = String(price.input);
    if (cached) cached.value = String(price.cached);
    if (output) output.value = String(price.output);
  }

  function syncSelectedPriceToState() {
    const key = selectedPriceKey();
    if (!key) return;
    modelPrices.set(key, {
      input: Number(document.getElementById("model-price-input")?.value || 0) || 0,
      cached: Number(document.getElementById("model-price-cached")?.value || 0) || 0,
      output: Number(document.getElementById("model-price-output")?.value || 0) || 0,
    });
  }

  function taskAiCost(row, policy, prices) {
    const price = prices.get(row.dataset.priceKey || row.dataset.model || "") || { input: 0, cached: 0, output: 0 };
    const cachedTokens = Number(row.dataset.cachedInputTokens || 0) || 0;
    const uncachedTokens = Number(row.dataset.uncachedInputTokens || 0) || 0;
    const outputTokens = Number(row.dataset.outputTokens || 0) || 0;
    const uncached = uncachedTokens / 1_000_000 * price.input * policy.usdToCny;
    const cached = cachedTokens / 1_000_000 * price.cached * policy.usdToCny;
    const output = outputTokens / 1_000_000 * price.output * policy.usdToCny;
    return {
      ai: uncached + cached + output,
      input: uncached + cached,
      output,
      cached,
      uncached,
    };
  }

  function recalculateCosts() {
    const policy = readPolicy();
    const prices = readModelPrices();
    const totals = { ai: 0, input: 0, output: 0, cached: 0, uncached: 0, traditional: 0, savings: 0 };
    const byModel = new Map();

    taskRows.forEach((row) => {
      const complexity = row.dataset.complexity || "unknown";
      const model = row.dataset.model || "unknown";
      const aiCost = taskAiCost(row, policy, prices);
      const ai = aiCost.ai;
      const input = aiCost.input;
      const output = aiCost.output;
      const cached = aiCost.cached;
      const uncached = aiCost.uncached;
      const traditional = policy.hourly * Number(policy.hours[complexity] ?? policy.hours.unknown ?? 0);
      const savings = traditional - ai;
      const roi = ai > 0 ? savings / ai * 100 : null;

      row.querySelector('[data-cost-field="ai"]').textContent = money(ai);
      row.querySelector('[data-cost-field="input"]').textContent = money(input);
      row.querySelector('[data-cost-field="output"]').textContent = money(output);
      row.querySelector('[data-cost-field="savings"]').textContent = money(savings);
      row.querySelector('[data-cost-field="roi"]').textContent = pct(roi);

      totals.ai += ai;
      totals.input += input;
      totals.output += output;
      totals.cached += cached;
      totals.uncached += uncached;
      totals.traditional += traditional;
      totals.savings += savings;

      const entry = byModel.get(model) || { turns: 0, ai: 0, input: 0, output: 0, traditional: 0, savings: 0 };
      entry.turns += 1;
      entry.ai += ai;
      entry.input += input;
      entry.output += output;
      entry.traditional += traditional;
      entry.savings += savings;
      byModel.set(model, entry);
    });

    const roi = totals.ai > 0 ? totals.savings / totals.ai * 100 : null;
    setText("metric-ai-cost", money(totals.ai));
    setText("metric-traditional-cost", money(totals.traditional));
    setText("metric-savings", money(totals.savings));
    setText("metric-roi", pct(roi));
    setText("metric-traditional-note", `工程师 ¥${number(policy.hourly)}/h`);
    setText("cost-panel-ai", money(totals.ai));
    setText("cost-panel-traditional", money(totals.traditional));
    setText("cost-panel-savings", money(totals.savings));
    setText("cost-panel-input", money(totals.input));
    setText("cost-panel-output", money(totals.output));
    setText("cost-panel-cached", money(totals.cached));
    setText("cost-panel-uncached", money(totals.uncached));
    const maxCost = Math.max(totals.ai, totals.traditional, 1);
    setWidth("ai-cost-bar", totals.ai, maxCost);
    setWidth("traditional-cost-bar", totals.traditional, maxCost);

    const body = document.getElementById("model-cost-body");
    if (body) {
      body.innerHTML = Array.from(byModel.entries()).sort(([a], [b]) => a.localeCompare(b)).map(([model, item]) => {
        const modelRoi = item.ai > 0 ? item.savings / item.ai * 100 : null;
        return `<tr>
          <td>${esc(model)}</td>
          <td>${number(item.turns)}</td>
          <td>${money(item.ai)}</td>
          <td>${money(item.input)}</td>
          <td>${money(item.output)}</td>
          <td>${money(item.traditional)}</td>
          <td>${money(item.savings)}</td>
          <td>${pct(modelRoi)}</td>
        </tr>`;
      }).join("");
    }
  }

  function renderPeriodUsage() {
    const list = document.getElementById("period-usage-list");
    if (!list) return;
    const rows = (data.periodUsage && data.periodUsage[state.period]) || [];
    const maxTotal = Math.max(1, ...rows.map((item) => Number(item.token_usage?.total_tokens || 0)));
    list.innerHTML = rows.map((item) => {
      const usage = item.token_usage || {};
      const total = Number(usage.total_tokens || 0);
      const cached = Number(usage.cached_input_tokens || 0);
      const uncached = Number(usage.uncached_input_tokens || 0);
      const output = Number(usage.output_tokens || 0);
      const scale = total > 0 ? Math.max(5, total / maxTotal * 100) : 0;
      const innerTotal = Math.max(cached + uncached + output, 1);
      return `<div class="usage-row">
        <span>${esc(item.label)}</span>
        <div class="usage-bar" style="width:${scale.toFixed(2)}%">
          <i class="cached" style="width:${(cached / innerTotal * 100).toFixed(2)}%"></i>
          <i class="uncached" style="width:${(uncached / innerTotal * 100).toFixed(2)}%"></i>
          <i class="output" style="width:${(output / innerTotal * 100).toFixed(2)}%"></i>
        </div>
        <strong class="usage-value">${number(total)} / ${number(item.turns)} 轮</strong>
      </div>`;
    }).join("") || '<div class="usage-row"><span>-</span><div></div><strong class="usage-value">0</strong></div>';
  }

  function renderPagination() {
    const pageSizeSelect = document.getElementById("task-page-size");
    state.pageSize = Number(pageSizeSelect?.value || state.pageSize) || 5;
    const pageCount = Math.max(1, Math.ceil(taskRows.length / state.pageSize));
    state.page = Math.max(1, Math.min(state.page, pageCount));
    const start = (state.page - 1) * state.pageSize;
    const end = start + state.pageSize;
    taskRows.forEach((row, index) => {
      row.style.display = index >= start && index < end ? "" : "none";
    });
    setText("task-page-info", `${state.page} / ${pageCount}`);
    const prev = document.getElementById("task-prev");
    const next = document.getElementById("task-next");
    if (prev) prev.disabled = state.page <= 1;
    if (next) next.disabled = state.page >= pageCount;
  }

  document.querySelectorAll(".control-grid input").forEach((input) => {
    input.addEventListener("input", recalculateCosts);
  });
  document.getElementById("model-price-select")?.addEventListener("change", () => {
    syncPriceInputsFromState();
    recalculateCosts();
  });
  ["model-price-input", "model-price-cached", "model-price-output"].forEach((id) => {
    document.getElementById(id)?.addEventListener("input", () => {
      syncSelectedPriceToState();
      recalculateCosts();
    });
  });
  document.querySelectorAll("[data-period]").forEach((button) => {
    button.addEventListener("click", () => {
      state.period = button.dataset.period || "daily";
      document.querySelectorAll("[data-period]").forEach((item) => item.classList.toggle("active", item === button));
      renderPeriodUsage();
    });
  });
  document.getElementById("task-page-size")?.addEventListener("change", () => {
    state.page = 1;
    renderPagination();
  });
  document.getElementById("task-prev")?.addEventListener("click", () => {
    state.page -= 1;
    renderPagination();
  });
  document.getElementById("task-next")?.addEventListener("click", () => {
    state.page += 1;
    renderPagination();
  });

  syncPriceInputsFromState();
  recalculateCosts();
  renderPeriodUsage();
  renderPagination();
})();
</script>
"""


def render_task_rows(tasks: list[dict[str, Any]]) -> str:
    max_savings = max(
        (float((item.get("cost_estimate") or {}).get("replacement_savings_cny") or 0.0) for item in tasks),
        default=1.0,
    )
    rows = []
    for item in tasks:
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        cost = item.get("cost_estimate") if isinstance(item.get("cost_estimate"), dict) else {}
        tokens = normalize_usage(item.get("token_usage") if isinstance(item.get("token_usage"), dict) else {})
        timing = item.get("timing") if isinstance(item.get("timing"), dict) else {}
        git = item.get("git") if isinstance(item.get("git"), dict) else {}
        savings = float(cost.get("replacement_savings_cny") or 0.0)
        model = infer_model(item)
        complexity = str(task.get("complexity") or "unknown")
        category = str(task.get("category") or "other")
        price_key = str(cost.get("model_price_key") or model)
        rows.append(
            f"""
            <article class="task-row"
              data-model="{escape(model)}"
              data-price-key="{escape(price_key)}"
              data-complexity="{escape(complexity)}"
              data-input-tokens="{escape(tokens.get("input_tokens") or 0)}"
              data-cached-input-tokens="{escape(tokens.get("cached_input_tokens") or 0)}"
              data-uncached-input-tokens="{escape(tokens.get("uncached_input_tokens") or 0)}"
              data-output-tokens="{escape(tokens.get("output_tokens") or 0)}"
              data-ai-cost="{escape(cost.get("ai_cost_cny") or 0)}"
              data-input-cost="{escape(cost.get("input_cost_cny") or 0)}"
              data-output-cost="{escape(cost.get("output_cost_cny") or 0)}"
              data-cached-cost="{escape(cost.get("cached_input_cost_cny") or 0)}"
              data-uncached-cost="{escape(cost.get("uncached_input_cost_cny") or 0)}">
              <div class="task-main">
                <div class="task-kicker">
                  <span class="complexity-chip complexity-{escape(complexity)}">{escape(complexity_label(task.get("complexity")))}</span>
                  <span>{escape(category_label(category))}</span>
                  <span>{escape(status_label(task.get("status")))}</span>
                  <span>{escape(model)}</span>
                  <span title="{escape(git_label_title(git))}">{escape(git_label(git))}</span>
                </div>
                <h3>{escape(task_title(item))}</h3>
                <p>{escape(task.get("summary") or "")}</p>
                <blockquote>{escape(task.get("business_value") or "")}</blockquote>
              </div>
              <div class="task-value">
                <div class="mini-bar"><i style="width: {css_width(savings, max_savings, floor=4)}"></i></div>
                <dl>
                  <div><dt>AI</dt><dd data-cost-field="ai">{money(cost.get("ai_cost_cny"))}</dd></div>
                  <div><dt>输入成本</dt><dd data-cost-field="input">{money(cost.get("input_cost_cny"))}</dd></div>
                  <div><dt>输出成本</dt><dd data-cost-field="output">{money(cost.get("output_cost_cny"))}</dd></div>
                  <div><dt>节约</dt><dd data-cost-field="savings">{money(cost.get("replacement_savings_cny"))}</dd></div>
                  <div><dt>ROI</dt><dd data-cost-field="roi">{pct(cost.get("roi_percent"))}</dd></div>
                  <div><dt>累计输入Token</dt><dd>{number(tokens.get("input_tokens"))}</dd></div>
                  <div><dt>缓存输入Token</dt><dd>{number(tokens.get("cached_input_tokens"))}</dd></div>
                  <div><dt>非缓存输入Token</dt><dd>{number(tokens.get("uncached_input_tokens"))}</dd></div>
                  <div><dt>累计输出Token</dt><dd>{number(tokens.get("output_tokens"))}</dd></div>
                  <div><dt>推理输出Token</dt><dd>{number(tokens.get("reasoning_output_tokens"))}</dd></div>
                  <div><dt>累计总Token</dt><dd>{number(tokens.get("total_tokens"))}</dd></div>
                  <div><dt>耗时</dt><dd>{escape(fmt_duration(timing.get("elapsed_seconds")))}</dd></div>
                  <div><dt>轮次</dt><dd title="{escape(turn_indices_note(item))}">{number(task_turn_count(item))}</dd></div>
                </dl>
              </div>
            </article>
            """
        )
    return "\n".join(rows)


def build_html(
    usage_dir: Path,
    output: Path,
    project_summary: dict[str, Any],
    summary: dict[str, Any],
    value_report: dict[str, Any],
    value_source: str,
) -> str:
    project = str(project_summary.get("project") or value_report.get("project") or "project")
    task_metrics = project_summary.get("task_metrics") if isinstance(project_summary.get("task_metrics"), dict) else {}
    turn_metrics = project_summary.get("turn_metrics") if isinstance(project_summary.get("turn_metrics"), dict) else {}
    cost_totals = value_report.get("cost_totals") if isinstance(value_report.get("cost_totals"), dict) else {}
    cost_by_model = value_report.get("cost_by_model") if isinstance(value_report.get("cost_by_model"), dict) else {}
    tasks = sorted_tasks(value_report)
    task_tokens = task_metrics.get("token_totals") if isinstance(task_metrics.get("token_totals"), dict) else {}
    turn_tokens = turn_metrics.get("token_totals") if isinstance(turn_metrics.get("token_totals"), dict) else summary.get("token_totals", {})
    source_updated = project_summary.get("updated_at") or value_report.get("source_summary_updated_at")
    report_generated = value_report.get("generated_at") or utc_now()
    task_count = int(value_report.get("recorded_tasks") or task_metrics.get("recorded_tasks") or len(tasks))
    recorded_turns = int((turn_metrics or {}).get("recorded_turns") or summary.get("recorded_turns") or 0)
    closed_loops = int(task_metrics.get("git_closed_loops") or 0)
    total_hours = float(task_metrics.get("elapsed_seconds_total") or 0.0) / 3600
    complexity_counts = task_metrics.get("complexity_counts") if isinstance(task_metrics.get("complexity_counts"), dict) else {}
    category_counts = task_metrics.get("category_counts") if isinstance(task_metrics.get("category_counts"), dict) else {}
    status_counts = task_metrics.get("status_counts") if isinstance(task_metrics.get("status_counts"), dict) else {}
    cost_policy_data = value_report.get("cost_policy") if isinstance(value_report.get("cost_policy"), dict) else {}
    report_data = {
        "costPolicy": cost_policy_data,
        "modelPrices": model_prices_for_tasks(cost_policy_data, tasks),
        "periodUsage": period_usage_from_records(read_jsonl_records(usage_dir / "codex-turns.jsonl")),
    }

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(project)} AI Agent 价值报告</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18212f;
      --muted: #5b6778;
      --line: #dbe3ee;
      --paper: #f7f9fc;
      --surface: #ffffff;
      --green: #16825d;
      --blue: #2563eb;
      --amber: #d97706;
      --teal: #0f9f9a;
      --violet: #6d5bd0;
      --shadow: 0 18px 50px rgba(25, 39, 63, 0.10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, "Noto Sans SC", "Microsoft YaHei", Arial, sans-serif;
      color: var(--ink);
      background: var(--paper);
      letter-spacing: 0;
    }}
    main {{ min-width: 320px; }}
    .hero {{
      background:
        linear-gradient(120deg, rgba(22,130,93,.14), rgba(37,99,235,.10) 48%, rgba(217,119,6,.11)),
        #eef4f8;
      border-bottom: 1px solid var(--line);
    }}
    .wrap {{ width: min(1180px, calc(100vw - 40px)); margin: 0 auto; }}
    .hero .wrap {{
      display: grid;
      grid-template-columns: 1.15fr .85fr;
      gap: 28px;
      align-items: stretch;
      padding: 34px 0 28px;
    }}
    .title-block {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      min-height: 260px;
    }}
    .eyebrow {{
      color: var(--green);
      font-size: 13px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 12px 0 12px;
      font-size: clamp(34px, 5vw, 58px);
      line-height: 1.02;
      max-width: 780px;
    }}
    .subtitle {{
      margin: 0;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.7;
      max-width: 760px;
    }}
    .meta-line {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 22px;
      color: var(--muted);
      font-size: 13px;
    }}
    .meta-line span {{
      border: 1px solid rgba(91,103,120,.26);
      background: rgba(255,255,255,.62);
      padding: 8px 10px;
      border-radius: 8px;
    }}
    .scoreboard {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-content: center;
    }}
    .metric {{
      min-width: 0;
      min-height: 118px;
      padding: 18px;
      border: 1px solid rgba(219,227,238,.92);
      border-radius: 8px;
      background: rgba(255,255,255,.84);
      box-shadow: var(--shadow);
    }}
    .metric span, .metric small {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.35;
    }}
    .metric strong {{
      display: block;
      margin: 8px 0 5px;
      font-size: clamp(24px, 3.5vw, 34px);
      line-height: 1;
    }}
    .band {{ padding: 24px 0; }}
    .grid-3 {{
      display: grid;
      grid-template-columns: 1.25fr 1fr 1fr;
      gap: 18px;
    }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
      margin-top: 18px;
    }}
    .grid-3 > *, .grid-2 > * {{ min-width: 0; }}
    .panel {{
      min-width: 0;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: 0 10px 28px rgba(30, 43, 62, .06);
    }}
    .panel-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 16px;
    }}
    .panel h2 {{ margin: 0; font-size: 18px; line-height: 1.2; }}
    .panel p {{ margin: 0; color: var(--muted); font-size: 13px; }}
    .cost-bars {{ display: grid; gap: 14px; }}
    .cost-row {{
      display: grid;
      grid-template-columns: 82px 1fr minmax(95px, auto);
      align-items: center;
      gap: 12px;
      font-size: 14px;
    }}
    .cost-row strong {{ text-align: right; }}
    .bar-track, .dist-track, .mini-bar {{
      height: 13px;
      border-radius: 999px;
      background: #e8edf4;
      overflow: hidden;
    }}
    .bar-track i, .dist-track i, .mini-bar i {{
      display: block;
      height: 100%;
      border-radius: inherit;
    }}
    .bar-track.ai i {{ background: var(--green); }}
    .bar-track.traditional i {{ background: var(--amber); }}
    .saving-line {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 18px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }}
    .saving-line strong {{ color: var(--green); font-size: 22px; }}
    .cost-breakdown {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .cost-breakdown div {{ padding: 10px; border-radius: 8px; background: #f2f6fa; }}
    .cost-breakdown span {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }}
    .cost-breakdown strong {{ font-size: 15px; }}
    .token-stack {{
      display: flex;
      height: 34px;
      overflow: hidden;
      border-radius: 8px;
      background: #e8edf4;
    }}
    .token-stack i {{ min-width: 1px; }}
    .cached {{ background: var(--blue); }}
    .uncached {{ background: var(--teal); }}
    .output {{ background: var(--violet); }}
    .legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px 16px;
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
    .dot {{
      display: inline-block;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      margin-right: 6px;
      vertical-align: 1px;
    }}
    .dist-list {{ display: grid; gap: 12px; }}
    .dist-row {{
      display: grid;
      grid-template-columns: minmax(56px, 90px) 1fr 42px;
      align-items: center;
      gap: 10px;
      font-size: 14px;
    }}
    .dist-track i {{ background: var(--blue); }}
    .control-panel label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .control-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .price-controls {{
      display: grid;
      gap: 10px;
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }}
    .price-controls h3 {{
      margin: 0;
      font-size: 14px;
      line-height: 1.2;
    }}
    .price-editor {{
      display: grid;
      grid-template-columns: minmax(120px, 1fr) repeat(3, minmax(0, 1fr));
      gap: 10px;
      align-items: start;
    }}
    .price-editor label {{
      grid-template-rows: 28px 38px;
      gap: 4px;
    }}
    .price-editor label span {{
      display: block;
      min-height: 28px;
      line-height: 14px;
      overflow-wrap: anywhere;
    }}
    input, select, button {{
      font: inherit;
      color: var(--ink);
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 9px 10px;
      min-height: 38px;
    }}
    .segmented {{
      display: inline-flex;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #eef3f7;
    }}
    .segmented button, .pager button {{
      border: 0;
      border-radius: 6px;
      background: transparent;
      padding: 7px 10px;
      cursor: pointer;
    }}
    .segmented button.active, .pager button:not(:disabled):hover {{
      background: var(--surface);
      box-shadow: 0 1px 5px rgba(30, 43, 62, .12);
    }}
    .usage-list {{ display: grid; gap: 11px; }}
    .usage-row {{
      display: grid;
      grid-template-columns: minmax(88px, 130px) 1fr minmax(105px, auto);
      align-items: center;
      gap: 12px;
      font-size: 14px;
    }}
    .usage-bar {{
      display: flex;
      height: 16px;
      overflow: hidden;
      border-radius: 999px;
      background: #e8edf4;
    }}
    .usage-bar i {{ min-width: 1px; }}
    .usage-value {{ text-align: right; color: var(--muted); }}
    .table-wrap {{ width: 100%; max-width: 100%; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; min-width: 780px; }}
    th, td {{
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      white-space: nowrap;
    }}
    th {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
    .section-head {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 20px;
      margin: 12px 0 16px;
    }}
    .section-head h2 {{ margin: 0; font-size: 24px; }}
    .section-head p {{ margin: 0; color: var(--muted); font-size: 13px; }}
    .tasks {{ display: grid; gap: 14px; }}
    .task-toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }}
    .task-toolbar label {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .task-toolbar select {{ width: auto; min-width: 92px; }}
    .pager {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }}
    .pager button {{
      border: 1px solid var(--line);
      background: #fff;
    }}
    .pager button:disabled {{
      cursor: not-allowed;
      opacity: .45;
    }}
    .task-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 330px;
      gap: 18px;
      padding: 18px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 10px 26px rgba(30, 43, 62, .05);
    }}
    .task-main, .task-value {{ min-width: 0; }}
    .task-kicker {{ display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 10px; }}
    .task-kicker span {{
      padding: 4px 8px;
      border-radius: 999px;
      background: #eef3f7;
      color: var(--muted);
      font-size: 12px;
      line-height: 1;
    }}
    .task-kicker .complexity-high {{
      background: #fee2e2;
      color: #991b1b;
    }}
    .task-kicker .complexity-medium {{
      background: #fef3c7;
      color: #92400e;
    }}
    .task-kicker .complexity-low {{
      background: #dcfce7;
      color: #166534;
    }}
    .task-kicker .complexity-unknown {{
      background: #e5e7eb;
      color: #4b5563;
    }}
    .task-main h3 {{ margin: 0 0 8px; font-size: 19px; line-height: 1.35; }}
    .task-main p {{ margin: 0; color: var(--ink); line-height: 1.65; }}
    .task-main blockquote {{
      margin: 12px 0 0;
      padding-left: 12px;
      border-left: 3px solid var(--green);
      color: var(--muted);
      line-height: 1.6;
    }}
    .task-value {{
      border-left: 1px solid var(--line);
      padding-left: 18px;
      min-width: 0;
    }}
    .mini-bar {{ height: 8px; margin-bottom: 12px; }}
    .mini-bar i {{ background: linear-gradient(90deg, var(--green), var(--blue)); }}
    dl {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px 14px;
      margin: 0;
    }}
    dt {{ color: var(--muted); font-size: 12px; }}
    dd {{ margin: 3px 0 0; font-weight: 750; overflow-wrap: anywhere; }}
    code {{
      color: var(--ink);
      background: #eef3f7;
      padding: 2px 5px;
      border-radius: 5px;
      font-family: "JetBrains Mono", Consolas, monospace;
    }}
    @media (max-width: 920px) {{
      .hero .wrap, .grid-3, .grid-2, .task-row {{ grid-template-columns: 1fr; }}
      .title-block {{ min-height: auto; }}
      .task-value {{
        border-left: 0;
        border-top: 1px solid var(--line);
        padding-left: 0;
        padding-top: 16px;
      }}
    }}
    @media (max-width: 620px) {{
      .wrap {{ width: min(100% - 24px, 1180px); }}
      .hero .wrap {{ padding-top: 24px; }}
      .scoreboard {{ grid-template-columns: 1fr; }}
      .control-grid {{ grid-template-columns: 1fr; }}
      .price-editor {{ grid-template-columns: 1fr; }}
      .usage-row {{ grid-template-columns: 1fr; gap: 6px; }}
      .usage-value {{ text-align: left; }}
      .cost-row {{ grid-template-columns: 1fr; gap: 7px; }}
      .cost-row strong {{ text-align: left; }}
      .section-head {{ display: block; }}
      dl {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 34px; }}
    }}
    @media print {{
      body {{ background: #fff; }}
      .panel, .metric, .task-row {{ box-shadow: none; }}
      .hero {{ background: #fff; }}
    }}
  </style>
</head>
<body>
<main>
  <section class="hero">
    <div class="wrap">
      <div class="title-block">
        <div class="eyebrow">AI Agent Value Report</div>
        <h1>{escape(project)} 工程价值报告</h1>
        <p class="subtitle">基于 usage 中的逐轮 token、任务级摘要、Git 闭环和当前成本策略，汇总 AI 接管带来的成本替代、节约额、ROI 与交付任务价值。</p>
        <div class="meta-line">
          <span>数据更新 {escape(fmt_time(source_updated))}</span>
          <span>报告生成 {escape(fmt_time(report_generated))}</span>
          <span>价值数据 {escape(value_source)}</span>
        </div>
      </div>
      <div class="scoreboard">
        {render_metric("AI 成本", money(cost_totals.get("ai_cost_cny")), f"{number(cost_totals.get('priced_turns'))} 个已计价任务", "metric-ai-cost")}
        {render_metric("传统成本", money(cost_totals.get("traditional_cost_cny_all")), f"工程师 ¥{float((value_report.get('cost_policy') or {}).get('engineer_hourly_rate_cny') or 300):.0f}/h", "metric-traditional-cost", "metric-traditional-note")}
        {render_metric("节约额", money(cost_totals.get("replacement_savings_cny")), "传统成本 - AI 成本", "metric-savings")}
        {render_metric("ROI", pct(cost_totals.get("roi_percent")), f"{task_count} 个任务级摘要", "metric-roi")}
      </div>
    </div>
  </section>

  <section class="band">
    <div class="wrap grid-3">
      {render_cost_compare(cost_totals)}
      {render_token_stack(task_tokens or turn_tokens)}
      {render_cost_controls(cost_policy_data, tasks)}
    </div>
    <div class="wrap grid-2">
      {render_period_usage_panel()}
      {render_distribution("复杂度", complexity_counts, {"high": "高", "medium": "中", "low": "低", "unknown": "未知"})}
    </div>
    <div class="wrap grid-2">
      {render_distribution("任务类别", category_counts, {"implementation": "实现", "debugging": "调试", "design": "设计", "review": "评审", "ops": "运维", "documentation": "文档", "maintenance": "维护", "other": "其它"})}
      {render_distribution("任务状态", status_counts, {"completed": "已完成"})}
    </div>
    <div class="wrap grid-2">
      {render_model_table(cost_by_model)}
      <section class="panel">
        <div class="panel-head">
          <h2>工程覆盖</h2>
          <p>task 与 turn 两层口径</p>
        </div>
        <div class="dist-list">
          <div class="dist-row"><span>任务摘要</span><div class="dist-track"><i style="width:100%"></i></div><b>{number(task_count)}</b></div>
          <div class="dist-row"><span>逐轮记录</span><div class="dist-track"><i style="width:{css_width(recorded_turns, max(recorded_turns, task_count, 1))}"></i></div><b>{number(recorded_turns)}</b></div>
          <div class="dist-row"><span>Git 闭环</span><div class="dist-track"><i style="width:{css_width(closed_loops, max(task_count, 1))}"></i></div><b>{number(closed_loops)}</b></div>
          <div class="dist-row"><span>任务耗时</span><div class="dist-track"><i style="width:{css_width(total_hours, max(total_hours, 1))}"></i></div><b>{number(total_hours, 1)}h</b></div>
        </div>
      </section>
    </div>
  </section>

  <section class="band">
    <div class="wrap">
      <div class="section-head">
        <h2>任务价值明细</h2>
        <p>{escape(project)} / {number(task_count)} tasks / {number((task_metrics.get("token_totals") or {}).get("total_tokens"))} task tokens</p>
      </div>
      <div class="task-toolbar">
        <label>每页
          <select id="task-page-size">
            <option value="3">3</option>
            <option value="5" selected>5</option>
            <option value="10">10</option>
            <option value="9999">全部</option>
          </select>
        </label>
        <div class="pager">
          <button type="button" id="task-prev">上一页</button>
          <span id="task-page-info">1 / 1</span>
          <button type="button" id="task-next">下一页</button>
        </div>
      </div>
      <div class="tasks">
        {render_task_rows(tasks)}
      </div>
    </div>
  </section>

</main>
<script type="application/json" id="report-data">{json_for_script(report_data)}</script>
{interactive_script()}
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    default_root = project_root_from_script()
    parser = argparse.ArgumentParser(description="Generate a static HTML value report from .agent/usage data.")
    parser.add_argument("--usage-dir", default=str(default_root / ".agent" / "usage"))
    parser.add_argument("--output", default=str(default_root / ".agent" / "usage" / "value-report.html"))
    parser.add_argument("--skip-project-hook", action="store_true", help="Use an existing value-report.json instead of calling the project hook.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    usage_dir = Path(args.usage_dir).expanduser().resolve()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    output = output.resolve()
    project_root = infer_project_root(usage_dir)

    project_summary = read_json(usage_dir / "project-summary.json")
    summary = maybe_read_json(usage_dir / "summary.json")
    value_report, value_source = load_value_report(usage_dir, project_root, project_summary, args.skip_project_hook)
    value_report = merge_task_value_history(project_summary, value_report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        build_html(usage_dir, output, project_summary, summary, value_report, value_source),
        encoding="utf-8",
    )
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
