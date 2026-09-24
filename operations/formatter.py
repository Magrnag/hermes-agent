"""Stable, bounded, secret-safe formatting for operation results."""

from __future__ import annotations

from typing import Any

from .models import StructuredResult
from .runner import redact

_STATUS_LABELS = {
    "queued": "待機中",
    "running": "実行中",
    "succeeded": "完了",
    "failed": "失敗",
    "retry": "再試行",
    "quota": "割当超過",
    "blocked": "ブロック",
    "cancelled": "キャンセル",
    "waiting_human": "人間の確認待ち",
    "denied": "拒否",
    "coalesced": "統合済み",
}


def _safe(value: Any, limit: int = 500) -> str:
    return redact(str(value))[:limit]


def format_result(result: StructuredResult | dict[str, Any]) -> str:
    """Render a Japanese operations card without raw output or secret-bearing fields."""
    item = (
        result
        if isinstance(result, StructuredResult)
        else StructuredResult.model_validate(result)
    )
    prefix = (
        "成功"
        if item.ok
        else "確認待ち"
        if item.requires_human
        else "拒否"
        if item.permission.value == "denied"
        else "受付"
        if item.status and item.status.value == "queued"
        else "失敗"
    )
    lines = [f"{prefix}: {_safe(item.title)}"]
    if item.message:
        lines.append(_safe(item.message, 1000))
    if item.run_id:
        lines.append(f"実行ID: {_safe(item.run_id, 128)}")
    if item.status:
        lines.append(f"状態: {_STATUS_LABELS.get(item.status.value, '不明')}")

    details = item.details
    if "running" in details:
        lines.append(
            f"実行中: {int(details['running'])} / 記録: {int(details.get('runs', 0))}"
        )
    jobs = details.get("jobs")
    if isinstance(jobs, list):
        for job in jobs[:20]:
            if not isinstance(job, dict):
                continue
            job_id = job.get("job_id") or job.get("id") or "unknown"
            name = job.get("name")
            label = f"{name} ({job_id})" if name else str(job_id)
            state = "有効" if job.get("enabled") else "無効"
            manual = "手動可" if job.get("allow_manual_run") else "参照のみ"
            lines.append(
                f"- {_safe(label, 180)}: {state}, {manual}, "
                f"次回={_safe(job.get('next_run_at') or 'なし', 100)}"
            )
    runs = details.get("runs")
    if isinstance(runs, list):
        for run in runs[:20]:
            if not isinstance(run, dict):
                continue
            lines.append(
                f"- {_safe(run.get('run_id', 'unknown'), 128)}: "
                f"{_safe(run.get('status', 'unknown'), 40)} "
                f"{_safe(run.get('action', ''), 40)} {_safe(run.get('target') or '', 100)}"
            )
    for key in ("job_id", "workspace", "log_available"):
        if key in details:
            lines.append(f"{key}: {_safe(details[key])}")
    if "confirmation" in details:
        lines.append(
            f"確認コード: {_safe(details['confirmation'], 200)} "
            "を付けて同じ操作を再送してください"
        )
    return "\n".join(lines)[:6000]
