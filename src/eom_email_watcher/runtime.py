from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config, load_config, secure_runtime_paths
from .db import Store
from .gmail import GmailGateway
from .mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxSession,
)
from .model import GatewayModel, LocalModel, ModelRuntime


@dataclass(frozen=True)
class Runtime:
    config: Config
    store: Store
    model: ModelRuntime


def configured_mailbox_identity(_config: Config) -> tuple[str, str]:
    """Return the configured provider/account without opening a provider connection."""
    return DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID


def load_configured_mailbox(config: Config) -> MailboxSession:
    """Load the currently configured mailbox behind the provider-neutral boundary."""
    provider, account_id = configured_mailbox_identity(config)
    return MailboxSession(
        provider,
        account_id,
        GmailGateway.from_token(config.gmail_credentials_file, config.gmail_token_file),
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
