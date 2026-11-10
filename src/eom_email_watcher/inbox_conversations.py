"""Staged Inbox conversation projection; existing identity/filter/removal owners decide."""

import math
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import thread_reads
from .config import exact_sender_selector_id, normalize_validated_address
from .db import Store, _inbox_filter_predicates
from .mime import body_was_truncated

Priority = Literal["urgent", "high", "normal", "low", "untriaged"]
Category = Literal[
    "invoice",
    "scheduling",
    "customer_request",
    "automated_notice",
    "informational",
    "other",
    "unclassified",
]
Status = Literal["pending", "analyzed", "summarized", "skipped"]
INBOX_PRIORITIES = frozenset(get_args(Priority))
INBOX_CATEGORIES = frozenset(get_args(Category))
INBOX_STATUSES = frozenset(get_args(Status))
Count = Annotated[int, Field(ge=0, le=2**53 - 1)]
Text = Annotated[str, Field(min_length=1)]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Query(Closed):
    limit: Annotated[int, Field(ge=1, le=100)] = 25
    cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    provider: Annotated[str, Field(min_length=1, max_length=64, pattern=r"\S")] | None = None
    account_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"\S")] | None = None
    sender: Annotated[str, Field(min_length=1, pattern=r"\S")] | None = None
    sender_query: Annotated[str, Field(min_length=1, max_length=320, pattern=r"\S")] | None = None
    keyword: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")] | None = None
    priority: Priority | None = None
    category: Category | None = None
    status: Status | None = None

    @field_validator("provider", "account_id", "sender_query", "keyword")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if value is not None else None

    @field_validator("sender")
    @classmethod
    def exact_sender(cls, value):
        if value is not None:
            exact_sender_selector_id(value)
            return normalize_validated_address(value)
        return None


class ThreadRef(Closed):
    provider: Text
    account_id: Text
    mailbox_identity_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    thread_key: Text


class MessageRef(Closed):
    provider: Text
    account_id: Text
    mailbox_identity_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    message_id: Text


class Header(Closed):
    message_ref: MessageRef
    subject: str
    sender: str
    sender_name: str | None
    received_at: str | None


class Sender(Closed):
    address: str
    name: str | None


class Counts(Closed):
    messages: Annotated[Count, Field(ge=1)]
    matching_messages: Annotated[Count, Field(ge=1)]
    pending_messages: Count
    analysis_errors: Count
    action_required: Count
    attachments: Count
    truncated_messages: Count
    unknown_truncation_messages: Count

    @model_validator(mode="after")
    def member_counts(self):
        if any(
            getattr(self, name) > self.messages
            for name in (
                "matching_messages",
                "pending_messages",
                "analysis_errors",
                "action_required",
                "truncated_messages",
                "unknown_truncation_messages",
            )
        ):
            raise ValueError("member count exceeds conversation size")
        if self.truncated_messages + self.unknown_truncation_messages > self.messages:
            raise ValueError("truncation counts overlap")
        return self


class Conversation(Closed):
    thread_ref: ThreadRef
    latest: Header
    matching_message_ref: MessageRef
    senders: Annotated[list[Sender], Field(min_length=1)]
    counts: Counts

    @model_validator(mode="after")
    def scoped_members(self):
        scope = self.thread_ref.model_dump(exclude={"thread_key"})
        if any(
            ref.model_dump(exclude={"message_id"}) != scope
            for ref in (
                self.latest.message_ref,
                self.matching_message_ref,
            )
        ):
            raise ValueError("conversation member scope mismatch")
        senders = [(sender.address, sender.name) for sender in self.senders]
        if (
            len(senders) != len(set(senders))
            or (self.latest.sender, self.latest.sender_name) not in senders
        ):
            raise ValueError("inconsistent sender projection")
        return self


class Totals(Closed):
    conversations: Count
    messages: Count
    matching_messages: Count


class Page(Closed):
    kind: Literal["inbox_conversations_v1"]
    items: Annotated[list[Conversation], Field(max_length=100)]
    next_cursor: Annotated[str, Field(min_length=1, max_length=4096)] | None
    totals: Totals

    @model_validator(mode="after")
    def page_counts(self):
        refs = [item.thread_ref.model_dump_json() for item in self.items]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate conversation")
        if (
            not self.totals.messages >= self.totals.matching_messages >= self.totals.conversations
            or self.totals.conversations == 0
            and self.totals.messages != 0
        ):
            raise ValueError("inconsistent totals")
        if (
            len(self.items) > self.totals.conversations
            or sum(item.counts.messages for item in self.items) > self.totals.messages
            or sum(item.counts.matching_messages for item in self.items)
            > self.totals.matching_messages
            or not self.items
            and self.next_cursor is not None
        ):
            raise ValueError("page exceeds totals")
        return self


def _origin(ref: thread_reads.ThreadRef, message_id: str) -> dict:
    return asdict(
        thread_reads.InboxMessageRef(
            ref.provider,
            ref.account_id,
            ref.mailbox_identity_key,
            message_id,
        )
    )


def query(store: Store, request: Query) -> dict:
    filters = request.model_dump(exclude={"cursor", "limit"})
    with thread_reads.snapshot(store) as db:
        clauses, values = _inbox_filter_predicates(**filters)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        matching = {
            r[0]
            for r in db.execute(
                "SELECT message_id FROM logical_messages m" + where,
                values,
            )
        }
        scope, parameters = _inbox_filter_predicates(
            provider=request.provider,
            account_id=request.account_id,
        )
        where = " WHERE " + " AND ".join(scope) if scope else ""
        metadata = {
            r["message_id"]: r
            for r in db.execute(
                """SELECT m.message_id,m.status,m.last_error,m.action_required,
            m.analysis_body_chars,m.analysis_body_source_chars,
            (SELECT COUNT(*) FROM message_attachments a WHERE a.message_id=m.message_id)
            AS attachments FROM logical_messages m"""
                + where,
                parameters,
            )
        }
        provider = request.provider if request.account_id is not None else None
        grouped = thread_reads._members(db, provider, request.account_id)
        seen, projected, revision = set(), [], []
        for ref, all_members in grouped.items():
            members = [(row, order) for row, order in all_members if row["message_id"] in metadata]
            if not members:
                continue
            seen.update(row["message_id"] for row, _ in members)
            members.sort(key=lambda m: (*thread_reads._newest(m[1]), m[0]["message_id"]))
            hits = [m for m in members if m[0]["message_id"] in matching]
            revision.extend(
                (
                    dict(row),
                    thread_reads._newest(order),
                    dict(metadata[row["message_id"]]),
                    row["message_id"] in matching,
                )
                for row, order in members
            )
            if not hits:
                continue

            latest, order = members[0]
            counts = [metadata[row["message_id"]] for row, _ in members]
            truncation = [
                body_was_truncated(
                    m["analysis_body_chars"],
                    m["analysis_body_source_chars"],
                )
                for m in counts
            ]
            sender_pairs = {(row["sender"], row["sender_name"]) for row, _ in members}
            item = Conversation.model_validate(
                {
                    "thread_ref": asdict(ref),
                    "latest": {
                        "message_ref": _origin(ref, latest["message_id"]),
                        "subject": latest["subject"],
                        "sender": latest["sender"],
                        "sender_name": latest["sender_name"],
                        "received_at": datetime.fromtimestamp(order[0], UTC).isoformat()
                        if math.isfinite(order[0])
                        else None,
                    },
                    "matching_message_ref": _origin(ref, hits[0][0]["message_id"]),
                    "senders": [
                        {"address": a, "name": n}
                        for a, n in sorted(
                            sender_pairs,
                            key=lambda pair: (pair[0], pair[1] is not None, pair[1] or ""),
                        )
                    ],
                    "counts": {
                        "messages": len(members),
                        "matching_messages": len(hits),
                        "pending_messages": sum(m["status"] == "pending" for m in counts),
                        "analysis_errors": sum(m["last_error"] is not None for m in counts),
                        "action_required": sum(m["action_required"] == 1 for m in counts),
                        "attachments": sum(m["attachments"] for m in counts),
                        "truncated_messages": sum(value is True for value in truncation),
                        "unknown_truncation_messages": sum(value is None for value in truncation),
                    },
                }
            ).model_dump()
            key = (
                *thread_reads._newest(order),
                ref.provider,
                ref.account_id,
                ref.mailbox_identity_key is not None,
                ref.mailbox_identity_key or "",
                ref.thread_key,
                latest["message_id"],
            )
            projected.append((key, item))
        if seen != set(metadata):
            raise RuntimeError("Retained thread membership is inconsistent")
        projected.sort(key=lambda pair: pair[0])
        page = thread_reads._page(
            [item for _, item in projected],
            [key for key, _ in projected],
            selector={"operation": "inbox.conversations.query", **filters},
            revision=sorted(revision, key=lambda r: r[0]["message_id"]),
            limit=request.limit,
            cursor=request.cursor,
        )
        for item in page["items"]:
            thread_reads._inbox_thread(
                db,
                thread_reads.InboxMessageRef(
                    **item["latest"]["message_ref"],
                ),
            )
        return Page.model_validate(
            {
                "kind": "inbox_conversations_v1",
                **page,
                "totals": {
                    "conversations": len(projected),
                    "messages": sum(item["counts"]["messages"] for _, item in projected),
                    "matching_messages": sum(
                        item["counts"]["matching_messages"] for _, item in projected
                    ),
                },
            }
        ).model_dump()
