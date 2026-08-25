import pytest

from eom_email_watcher.model import Analysis
from eom_email_watcher.notifications import (
    ChannelResult,
    NotificationError,
    send_analysis,
    send_fallback,
)


def analysis(**overrides) -> Analysis:
    values = {
        "category": "invoice",
        "priority": "high",
        "summary": "An invoice is due.",
        "action_required": True,
        "suggested_action": "Pay it",
        "deadline_text": None,
        "deadline_iso": None,
        "confidence": 0.9,
    }
    values.update(overrides)
    return Analysis(**values)


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)


def test_desktop_only_when_ntfy_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    def fake_run(args, **kwargs):
        calls.append(args)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    ntfy_calls = []
    monkeypatch.setattr(
        "eom_email_watcher.notifications.httpx.post",
        lambda *a, **k: ntfy_calls.append((a, k)) or FakeResponse(),
    )

    result = send_analysis("Vendor", "Invoice", analysis())

    assert len(calls) == 1
    assert not ntfy_calls
    assert result.desktop == ChannelResult(attempted=True, delivered=True)
    assert result.ntfy == ChannelResult(attempted=False, delivered=False)


def test_both_channels_used_when_ntfy_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    class Result:
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    ntfy_calls = []

    def fake_post(url, *, json, timeout):
        ntfy_calls.append((url, json, timeout))
        return FakeResponse()

    monkeypatch.setattr("eom_email_watcher.notifications.httpx.post", fake_post)

    result = send_analysis(
        "Vendor",
        "Invoice",
        analysis(priority="urgent"),
        ntfy_topic="eom-email-watch-0123456789ab",
        ntfy_url="https://ntfy.sh",
    )

    assert len(ntfy_calls) == 1
    url, payload, timeout = ntfy_calls[0]
    assert url == "https://ntfy.sh"
    assert payload["topic"] == "eom-email-watch-0123456789ab"
    assert payload["priority"] == 5
    assert payload["title"] == "Vendor: Invoice"
    assert result.desktop.delivered
    assert result.ntfy.delivered
    assert result.failures == ()


def test_ntfy_failure_alone_does_not_raise_when_desktop_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    class Result:
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())

    def failing_post(*a, **k):
        import httpx

        raise httpx.ConnectError("refused")

    monkeypatch.setattr("eom_email_watcher.notifications.httpx.post", failing_post)

    # Must not raise -- the desktop channel got through.
    result = send_analysis(
        "Vendor", "Invoice", analysis(), ntfy_topic="eom-email-watch-0123456789ab"
    )
    assert result.desktop.delivered
    assert not result.ntfy.delivered
    assert result.ntfy.error == "ntfy delivery failed: ConnectError"


def test_desktop_timeout_falls_through_to_ntfy(monkeypatch: pytest.MonkeyPatch) -> None:
    # A hung notify-send raises subprocess.TimeoutExpired, not NotificationError.
    # It must not crash the whole run when another channel is configured.
    import subprocess

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    def timing_out(*a, **k):
        raise subprocess.TimeoutExpired(cmd="notify-send", timeout=10)

    monkeypatch.setattr("subprocess.run", timing_out)
    ntfy_calls = []
    monkeypatch.setattr(
        "eom_email_watcher.notifications.httpx.post",
        lambda *a, **k: ntfy_calls.append(1) or FakeResponse(),
    )

    result = send_analysis(
        "Vendor", "Invoice", analysis(), ntfy_topic="eom-email-watch-0123456789ab"
    )

    assert ntfy_calls == [1]
    assert not result.desktop.delivered
    assert result.desktop.error == "notify-send failed: TimeoutExpired"
    assert result.ntfy.delivered


def test_desktop_oserror_falls_through_to_ntfy(monkeypatch: pytest.MonkeyPatch) -> None:
    # notify-send existing per shutil.which but failing to spawn (e.g. permission
    # issue) raises OSError, not NotificationError.
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    def unspawnable(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr("subprocess.run", unspawnable)
    ntfy_calls = []
    monkeypatch.setattr(
        "eom_email_watcher.notifications.httpx.post",
        lambda *a, **k: ntfy_calls.append(1) or FakeResponse(),
    )

    send_analysis(
        "Vendor", "Invoice", analysis(), ntfy_topic="eom-email-watch-0123456789ab"
    )

    assert ntfy_calls == [1]


def test_desktop_timeout_with_no_other_channel_raises_notification_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="notify-send", timeout=10)
        ),
    )

    with pytest.raises(NotificationError):
        send_analysis("Vendor", "Invoice", analysis())


def test_all_channels_failing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)

    def failing_post(*a, **k):
        import httpx

        raise httpx.ConnectError("refused")

    monkeypatch.setattr("eom_email_watcher.notifications.httpx.post", failing_post)

    with pytest.raises(NotificationError):
        send_analysis(
            "Vendor", "Invoice", analysis(), ntfy_topic="eom-email-watch-0123456789ab"
        )


def test_fallback_reaches_both_channels_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/notify-send")

    class Result:
        returncode = 0

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    ntfy_calls = []
    monkeypatch.setattr(
        "eom_email_watcher.notifications.httpx.post",
        lambda *a, **k: ntfy_calls.append(1) or FakeResponse(),
    )

    send_fallback("Vendor", "Invoice", ntfy_topic="eom-email-watch-0123456789ab")

    assert ntfy_calls == [1]


def test_dry_run_prints_instead_of_sending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a, **k):
        raise AssertionError("should not be called in dry_run")

    monkeypatch.setattr("subprocess.run", boom)
    monkeypatch.setattr("eom_email_watcher.notifications.httpx.post", boom)

    send_analysis(
        "Vendor",
        "Invoice",
        analysis(),
        ntfy_topic="eom-email-watch-0123456789ab",
        dry_run=True,
    )

    out = capsys.readouterr().out
    assert "notification" in out
    assert "ntfy" in out
