from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msal
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .microsoft365 import (
    AUTHORIZATION_TIMEOUT_SECONDS,
    TOKEN_LOCK_TIMEOUT_SECONDS,
    Microsoft365Error,
    MicrosoftAuthorizationRejected,
    _authorization_result,
    _load_cache,
    _new_public_client,
    _profile_address,
    _write_private_cache,
    load_microsoft_public_client,
)
from .microsoft365 import (
    SCOPES as MAIL_READ_SCOPES,
)

CALENDAR_READ_PROFILE = "read"
CALENDAR_READ_SCOPES = ("Calendars.Read",)
_CONSENT_PENDING_ERROR_CODES = frozenset({65001, 90094, 90095})


class MicrosoftCalendarConsentPending(Microsoft365Error):
    """The user or tenant administrator must finish calendar consent."""


def _entra_error_codes(result: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    raw_codes = result.get("error_codes")
    if isinstance(raw_codes, list):
        codes.update(
            code for code in raw_codes if isinstance(code, int) and not isinstance(code, bool)
        )

    description = result.get("error_description")
    if isinstance(description, str):
        prefix = description.partition(":")[0].strip().casefold()
        if prefix.startswith("aadsts"):
            numeric_code = prefix.removeprefix("aadsts")
            if numeric_code.isascii() and numeric_code.isdigit():
                codes.add(int(numeric_code))
    return codes


@dataclass(frozen=True)
class MicrosoftPrincipal:
    home_account_id: str
    tenant_id: str
    object_id: str
    email_address: str

    @property
    def key(self) -> str:
        value = "\0".join((self.home_account_id, self.tenant_id, self.object_id))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_part(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        raise MicrosoftAuthorizationRejected(
            f"Microsoft authorization did not identify one immutable {name}"
        )
    return value


def _principal(result: dict[str, Any], account: object) -> MicrosoftPrincipal:
    if not isinstance(account, dict):
        raise MicrosoftAuthorizationRejected(
            "Microsoft authorization did not identify one immutable principal"
        )
    claims = result.get("id_token_claims")
    claim_values = claims if isinstance(claims, dict) else {}
    return MicrosoftPrincipal(
        home_account_id=_identity_part(account.get("home_account_id"), "home account"),
        tenant_id=_identity_part(
            claim_values.get("tid", account.get("realm")),
            "tenant",
        ),
        object_id=_identity_part(
            claim_values.get("oid", account.get("local_account_id")),
            "object",
        ),
        email_address=_profile_address(result, account),
    )


def _calendar_authorization_result(result: object) -> dict[str, Any]:
    if isinstance(result, dict) and not result.get("access_token"):
        error = str(result.get("error", "")).casefold()
        suberror = str(result.get("suberror", "")).casefold()
        pending_errors = {
            "authorization_pending",
            "consent_required",
            "interaction_required",
        }
        if (
            error in pending_errors
            or suberror in pending_errors
            or _entra_error_codes(result) & _CONSENT_PENDING_ERROR_CODES
        ):
            raise MicrosoftCalendarConsentPending(
                "Microsoft calendar consent requires user or administrator approval"
            )
    return _authorization_result(result)


def microsoft_mailbox_principal(
    credentials_file: Path,
    token_file: Path,
) -> MicrosoftPrincipal:
    """Resolve the immutable principal from the existing Mail.Read cache."""
    configuration = load_microsoft_public_client(credentials_file)
    if not token_file.is_file():
        raise MicrosoftAuthorizationRejected("Microsoft mailbox is not authorized")
    try:
        with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
            cache = _load_cache(token_file)
            application = _new_public_client(configuration, cache)
            accounts = application.get_accounts()
            if not isinstance(accounts, list) or len(accounts) != 1:
                raise MicrosoftAuthorizationRejected(
                    "Microsoft mailbox cache does not identify one principal"
                )
            result = _authorization_result(
                application.acquire_token_silent_with_error(
                    list(MAIL_READ_SCOPES),
                    account=accounts[0],
                )
            )
            principal = _principal(result, accounts[0])
            if cache.has_state_changed:
                _write_private_cache(token_file, cache)
    except FileLockTimeout as exc:
        raise Microsoft365Error("Microsoft mailbox cache is busy; retry") from exc
    except Microsoft365Error:
        raise
    except Exception as exc:
        raise Microsoft365Error("Microsoft mailbox authorization failed; retry") from exc
    return principal


class MicrosoftCalendarReadAuthorization:
    def __init__(self, principal: MicrosoftPrincipal):
        self.principal = principal

    @classmethod
    def from_token(
        cls,
        credentials_file: Path,
        token_file: Path,
    ) -> MicrosoftCalendarReadAuthorization:
        configuration = load_microsoft_public_client(credentials_file)
        if not token_file.is_file():
            raise MicrosoftAuthorizationRejected("Microsoft calendar is not authorized")
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                cache = _load_cache(token_file)
                application = _new_public_client(configuration, cache)
                accounts = application.get_accounts()
                if not isinstance(accounts, list) or len(accounts) != 1:
                    raise MicrosoftAuthorizationRejected(
                        "Microsoft calendar cache does not identify one principal"
                    )
                result = _authorization_result(
                    application.acquire_token_silent_with_error(
                        list(CALENDAR_READ_SCOPES),
                        account=accounts[0],
                    )
                )
                principal = _principal(result, accounts[0])
                if cache.has_state_changed:
                    _write_private_cache(token_file, cache)
        except FileLockTimeout as exc:
            raise Microsoft365Error("Microsoft calendar cache is busy; retry") from exc
        except Microsoft365Error:
            raise
        except Exception as exc:
            raise Microsoft365Error("Microsoft calendar authorization failed; retry") from exc
        return cls(principal)

    @classmethod
    def authorize_with_status(
        cls,
        credentials_file: Path,
        token_file: Path,
    ) -> tuple[MicrosoftCalendarReadAuthorization, bool]:
        configuration = load_microsoft_public_client(credentials_file)
        cache = msal.SerializableTokenCache()
        try:
            application = _new_public_client(configuration, cache)
            raw_result = application.acquire_token_interactive(
                list(CALENDAR_READ_SCOPES),
                prompt="select_account",
                timeout=AUTHORIZATION_TIMEOUT_SECONDS,
                port=0,
            )
            result = _calendar_authorization_result(raw_result)
            accounts = application.get_accounts()
            if not isinstance(accounts, list) or len(accounts) != 1:
                raise MicrosoftAuthorizationRejected(
                    "Microsoft calendar authorization did not identify one principal"
                )
            principal = _principal(result, accounts[0])
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                _write_private_cache(token_file, cache)
        except FileLockTimeout as exc:
            raise Microsoft365Error("Microsoft calendar cache is busy; retry") from exc
        except Microsoft365Error:
            raise
        except Exception as exc:
            raise Microsoft365Error("Microsoft calendar authorization failed; retry") from exc
        return cls(principal), True
