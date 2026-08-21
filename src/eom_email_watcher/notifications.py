from __future__ import annotations

import shutil
import subprocess

import httpx

from .model import Analysis

NTFY_TIMEOUT_SECONDS = 5.0

# ntfy's publish API requires a numeric priority (1=min .. 5=urgent) -- a
# string value like "urgent" is rejected wholesale with a misleading
# "request body must be valid JSON" error, not a field-specific one.
# Confirmed against the live API before wiring this in.
_NTFY_PRIORITY = {
    "urgent": 5,
    "high": 4,
    "normal": 3,
    "low": 2,
}


class NotificationError(RuntimeError):
    """No configured notification channel could deliver the message."""


def _send_desktop(title: str, body: str, urgency: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[notification:{urgency}] {title}\n{body}")
        return
    executable = shutil.which("notify-send")
    if not executable:
        raise NotificationError("notify-send is not installed")
    try:
        result = subprocess.run(
            [executable, "--app-name=EOM Email Watcher", f"--urgency={urgency}", title, body],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # A hung or unspawnable notify-send must not crash the whole run --
        # the caller falls back to any other configured channel (e.g. ntfy).
        raise NotificationError(f"notify-send failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise NotificationError("notify-send returned an error")


def _send_ntfy(
    topic: str, url: str, title: str, body: str, priority: int, dry_run: bool
) -> None:
    if dry_run:
        print(f"[ntfy:{priority}] {title}\n{body}")
        return
    try:
        response = httpx.post(
            url,
            json={"topic": topic, "title": title, "message": body, "priority": priority},
            timeout=NTFY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise NotificationError(f"ntfy delivery failed: {type(exc).__name__}") from exc


def _deliver(
    title: str,
    body: str,
    urgency: str,
    *,
    ntfy_topic: str | None,
    ntfy_url: str,
    ntfy_priority: int,
    dry_run: bool,
) -> None:
    # Each configured channel is independent: a down phone-push endpoint should
    # not silence the desktop popup, and vice versa. Only raise -- which the
    # caller treats as "nothing got through" and triggers a retry/fallback --
    # when every channel that was actually attempted failed.
    errors: list[str] = []
    attempted = 0

    attempted += 1
    try:
        _send_desktop(title, body, urgency, dry_run)
    except NotificationError as exc:
        errors.append(str(exc))

    if ntfy_topic:
        attempted += 1
        try:
            _send_ntfy(ntfy_topic, ntfy_url, title, body, ntfy_priority, dry_run)
        except NotificationError as exc:
            errors.append(str(exc))

    if errors and len(errors) == attempted:
        raise NotificationError("; ".join(errors))


def send_analysis(
    sender_label: str,
    subject: str,
    analysis: Analysis,
    *,
    ntfy_topic: str | None = None,
    ntfy_url: str = "https://ntfy.sh",
    dry_run: bool = False,
) -> None:
    urgency = "critical" if analysis.priority == "urgent" else "normal"
    lines = [analysis.summary]
    if analysis.suggested_action:
        lines.append(f"Next: {analysis.suggested_action}")
    if analysis.deadline_iso:
        lines.append(f"Deadline: {analysis.deadline_iso}")
    _deliver(
        f"{sender_label}: {subject}",
        "\n".join(lines),
        urgency,
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        ntfy_priority=_NTFY_PRIORITY.get(analysis.priority, 3),
        dry_run=dry_run,
    )


def send_fallback(
    sender_label: str,
    subject: str,
    *,
    ntfy_topic: str | None = None,
    ntfy_url: str = "https://ntfy.sh",
    dry_run: bool = False,
) -> None:
    _deliver(
        f"{sender_label}: {subject}",
        "A watched email arrived. Local summary unavailable; it will be retried.",
        "normal",
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        ntfy_priority=3,
        dry_run=dry_run,
    )
