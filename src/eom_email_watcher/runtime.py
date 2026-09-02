from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, load_config, secure_runtime_paths
from .db import MailAccount, Store
from .gmail import GmailGateway
from .mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxAccountUnavailable,
    MailboxSession,
)
from .model import GatewayModel, LocalModel, ModelRuntime


@dataclass(frozen=True)
class Runtime:
    config: Config
    store: Store
    model: ModelRuntime


MAIL_PROVIDER_NAMES = {DEFAULT_MAIL_PROVIDER: "Gmail"}
GMAIL_GENERATED_ACCOUNT_ID = re.compile(r"gmail-[0-9a-f]{32}\Z")


def mail_account_token_file(config: Config, account: MailAccount) -> Path:
    """Resolve a provider token without accepting a path from persisted account data."""
    if account.provider == DEFAULT_MAIL_PROVIDER and account.account_id == DEFAULT_MAIL_ACCOUNT_ID:
        return config.gmail_token_file
    if account.provider == DEFAULT_MAIL_PROVIDER and GMAIL_GENERATED_ACCOUNT_ID.fullmatch(
        account.account_id
    ):
        return (
            config.database_file.parent
            / "mail-accounts"
            / f"{account.account_id}.readonly-token.json"
        )
    raise MailboxAccountUnavailable("The selected email provider is not available in this build")


def mail_account_connected(config: Config, account: MailAccount) -> bool:
    try:
        return mail_account_token_file(config, account).is_file()
    except MailboxAccountUnavailable:
        return False


def configured_mailbox_identity(store: Store) -> tuple[str, str]:
    """Return the active provider/account without opening a provider connection."""
    account = store.active_mail_account()
    if account is None:
        raise MailboxAccountUnavailable("No active email account is configured")
    return account.provider, account.account_id


def load_configured_mailbox(config: Config, store: Store) -> MailboxSession:
    """Load the currently configured mailbox behind the provider-neutral boundary."""
    account = store.active_mail_account()
    if account is None:
        raise MailboxAccountUnavailable("No active email account is configured")
    token_file = mail_account_token_file(config, account)
    if not token_file.is_file():
        raise MailboxAccountUnavailable("The active email account is disconnected")
    return MailboxSession(
        account.provider,
        account.account_id,
        GmailGateway.from_token(config.gmail_credentials_file, token_file),
    )


def load_runtime(config_path: Path) -> Runtime:
    config = load_config(config_path)
    secure_runtime_paths(config)
    store = Store(config.database_file)
    store.initialize()
    if config.model_backend == "gateway":
        assert config.model_api_token_file is not None
        assert config.model_ca_file is not None
        model: ModelRuntime = GatewayModel(
            config.model_base_url,
            config.model_timeout_seconds,
            config.model_api_token_file,
            config.model_ca_file,
        )
    else:
        model = LocalModel(
            config.model_base_url,
            config.model_name,
            config.model_timeout_seconds,
            config.model_api_token_file,
            config.model_require_auth,
        )
    return Runtime(config=config, store=store, model=model)
