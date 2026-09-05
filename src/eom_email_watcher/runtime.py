from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, load_config, secure_runtime_paths
from .db import MailAccount, Store
from .gmail import GmailGateway, gmail_credentials_configured
from .imap import IMAP_PROVIDER, ImapGateway
from .mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxAccountUnavailable,
    MailboxSession,
)
from .microsoft365 import (
    MICROSOFT365_PROVIDER,
    Microsoft365Gateway,
    microsoft_credentials_configured,
)
from .model import GatewayModel, LocalModel, ModelRuntime


@dataclass(frozen=True)
class Runtime:
    config: Config
    store: Store
    model: ModelRuntime


MAIL_PROVIDER_NAMES = {
    DEFAULT_MAIL_PROVIDER: "Gmail",
    IMAP_PROVIDER: "Other mail server",
    MICROSOFT365_PROVIDER: "Microsoft 365",
}
GMAIL_GENERATED_ACCOUNT_ID = re.compile(r"gmail-[0-9a-f]{32}\Z")
IMAP_GENERATED_ACCOUNT_ID = re.compile(r"imap-[0-9a-f]{32}\Z")
MICROSOFT_GENERATED_ACCOUNT_ID = re.compile(r"microsoft365-[0-9a-f]{32}\Z")


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
    if account.provider == MICROSOFT365_PROVIDER and MICROSOFT_GENERATED_ACCOUNT_ID.fullmatch(
        account.account_id
    ):
        return (
            config.database_file.parent / "mail-accounts" / f"{account.account_id}.msal-cache.json"
        )
    if account.provider == IMAP_PROVIDER and IMAP_GENERATED_ACCOUNT_ID.fullmatch(
        account.account_id
    ):
        return (
            config.database_file.parent / "mail-accounts" / f"{account.account_id}.credentials.json"
        )
    raise MailboxAccountUnavailable("The selected email provider is not available in this build")


def mail_account_connected(config: Config, account: MailAccount) -> bool:
    try:
        return mail_account_token_file(config, account).is_file()
    except MailboxAccountUnavailable:
        return False


def mail_provider_connection_available(config: Config, provider: str) -> bool:
    if provider == DEFAULT_MAIL_PROVIDER:
        return gmail_credentials_configured(config.gmail_credentials_file)
    if provider == MICROSOFT365_PROVIDER:
        return microsoft_credentials_configured(config.microsoft_credentials_file)
    return provider == IMAP_PROVIDER


def configured_mailbox_identity(store: Store) -> tuple[str, str]:
    """Return the active provider/account without opening a provider connection."""
    account = store.active_mail_account()
    if account is None:
        raise MailboxAccountUnavailable("No active email account is configured")
    return account.provider, account.account_id


def load_mailbox_account(
    config: Config,
    store: Store,
    provider: str,
    account_id: str,
) -> MailboxSession:
    """Load one persisted mailbox account without changing the polling selection."""
    account = store.mail_account(provider, account_id)
    if account is None:
        raise MailboxAccountUnavailable("The selected email account is not configured")
    token_file = mail_account_token_file(config, account)
    if not token_file.is_file():
        raise MailboxAccountUnavailable("The selected email account is disconnected")
    if account.provider == DEFAULT_MAIL_PROVIDER:
        gateway = GmailGateway.from_token(config.gmail_credentials_file, token_file)
    elif account.provider == MICROSOFT365_PROVIDER:
        gateway = Microsoft365Gateway.from_token(config.microsoft_credentials_file, token_file)
    elif account.provider == IMAP_PROVIDER:
        gateway = ImapGateway.from_credentials_file(token_file)
    else:
        raise MailboxAccountUnavailable(
            "The selected email provider is not available in this build"
        )
    return MailboxSession(account.provider, account.account_id, gateway)


def load_configured_mailbox(config: Config, store: Store) -> MailboxSession:
    """Load the currently configured mailbox behind the provider-neutral boundary."""
    account = store.active_mail_account()
    if account is None:
        raise MailboxAccountUnavailable("No active email account is configured")
    token_file = mail_account_token_file(config, account)
    if not token_file.is_file():
        raise MailboxAccountUnavailable("The active email account is disconnected")
    return load_mailbox_account(config, store, account.provider, account.account_id)


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
