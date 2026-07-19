from __future__ import annotations

import shutil
import subprocess

from .model import Analysis


class NotificationError(RuntimeError):
    """Desktop notification could not be delivered."""


def _send(title: str, body: str, urgency: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[notification:{urgency}] {title}\n{body}")
        return
    executable = shutil.which("notify-send")
    if not executable:
        raise NotificationError("notify-send is not installed")
    result = subprocess.run(
        [executable, "--app-name=EOM Email Watcher", f"--urgency={urgency}", title, body],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise NotificationError("notify-send returned an error")


def send_analysis(
    sender_label: str, subject: str, analysis: Analysis, *, dry_run: bool = False
) -> None:
    urgency = "critical" if analysis.priority == "urgent" else "normal"
    lines = [analysis.summary]
    if analysis.suggested_action:
        lines.append(f"Next: {analysis.suggested_action}")
    if analysis.deadline_iso:
        lines.append(f"Deadline: {analysis.deadline_iso}")
    _send(f"{sender_label}: {subject}", "\n".join(lines), urgency, dry_run)


def send_fallback(sender_label: str, subject: str, *, dry_run: bool = False) -> None:
    _send(
        f"{sender_label}: {subject}",
        "A watched email arrived. Local summary unavailable; it will be retried.",
        "normal",
        dry_run,
    )
