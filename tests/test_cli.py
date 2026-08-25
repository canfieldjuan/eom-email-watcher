from pathlib import Path
from types import SimpleNamespace

from eom_email_watcher import cli
from eom_email_watcher.notifications import ChannelResult, DeliveryResult


def test_setup_reports_partial_notification_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    token = tmp_path / "token.json"
    token.touch()
    config = SimpleNamespace(
        gmail_token_file=token,
        gmail_credentials_file=tmp_path / "credentials.json",
        notifications_enabled=True,
        ntfy_topic="eom-email-watch-0123456789ab",
        ntfy_url="https://ntfy.sh",
    )
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr(cli.GmailGateway, "from_token", lambda *args: object())

    class FakeWatcher:
        def __init__(self, *args):
            pass

        def bootstrap(self) -> str:
            return "200"

    monkeypatch.setattr(cli, "Watcher", FakeWatcher)
    monkeypatch.setattr(
        cli,
        "send_fallback",
        lambda *args, **kwargs: DeliveryResult(
            desktop=ChannelResult(attempted=True, delivered=True),
            ntfy=ChannelResult(
                attempted=True,
                delivered=False,
                error="ntfy delivery failed: ConnectError",
            ),
        ),
    )

    assert cli._setup(tmp_path / "config.toml") == 0

    output = capsys.readouterr()
    assert "ntfy delivery failed: ConnectError" in output.err
    assert "Baseline history cursor saved (200)" in output.out
