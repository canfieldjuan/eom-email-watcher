# Source-header evidence prerequisite (F3)

Status: PROPOSED. Contract and plan only; implementation follows acceptance.

## Contract

### Root cause

At main `4a375a28c8ae5797cf0a30f3d76121ecd9b4fa92`, `Store.record_message_location` (`src/eom_email_watcher/db.py:9081-9088`) reads the canonical message's `capture_timezone` to decide whether a source's headers were fetched. Commit `4d3e44c0` introduced this proxy. Capture date context belongs to the logical message's historical capture; source header observations belong to an individual mailbox copy. Neither a retained message's later metadata fetch nor an empty To/Cc result updates this proxy. A different copy can also inherit the canonical copy's context despite its own headers remaining unfetched.

The isolated production-store probes reproduce both errors: a retained source fetched with empty or nonempty To is stamped, survives reopening, then loses its stamp on a complete folder-only observation; an unfetched duplicate receives a stamp after its canonical row is assigned capture context. These are prerequisites for reliable M2.3 discovery, not evidence of a current discovery implementation.

The owning product definition is [D-identity, recorded locations](../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys). Implementation must consume it without restating a separate rule.

### Required change surface

- `db.py`: schema-31 migration, one source observation owner, all capture and observation writers, logical-source ownership transitions, and logical-unit deletion.
- `tests/test_db.py`: port the minimal probes into repository tests and cover migration, source isolation, lifecycle, rollback, and concurrent observation order.
- `tests/test_service.py`: exercise real known-message folder observations against stored source evidence; verify no metadata request is introduced by polling.
- Thread-view contract: amend only the recorded-locations definition. M2 plan: reference this prerequisite and move its future schema numbers and backup names together.
- No desktop, provider transport, model, or public engine protocol change is required. The internal `record_message_location` header-evidence argument becomes required so callers explicitly identify whether a header fetch occurred.

### Explicit non-scope

No timezone backfill, recipient replacement, extra provider fetch, admission change, folder-history removal, body storage, vendor matching, coverage generation, claim extraction, dependency change, or unrelated refactor. F5 Microsoft routing and M2.2 implementation remain separate. Existing scheduling, analysis, notifications, cutoff rules, and Connect decisions retain their behavior.

### Assumptions and blockers

Schema 30 has no reliable independent source-header fact. Existing confirmation timestamps can contain the demonstrated false positive; recipient rows and capture context cannot distinguish every fetched copy. The migration therefore conservatively discards confirmation once and reconstructs no header evidence. This can increase future discovery fetches within its existing bounds, but migration itself performs no network or model calls.

Schema 31 is currently unimplemented but reserved by the accepted M2 plan. This prerequisite consumes 31; M2.2, M2.3, and M2.4 become 32, 33, and 34. M2.2's messages-rebuild backup becomes `pre-v32`, with source version 31. This renumbering changes no accepted milestone behavior or backup guarantees. Acceptance of this amendment is required before implementation.

## Implementation plan

1. Add `message_source_headers(message_id, provider, account_id, mailbox_identity_key, provider_message_id, observed_at)` in schema 31. The source tuple is the primary key; all fields are non-null, and the timestamp is nonempty. `message_id` associates the evidence with its logical unit. A row records a successful source-header observation; absence means unknown. It works even when no admitted location or recipient is returned.
2. In the migration transaction, create the table and its logical-message index and deletion cleanup; clear existing location confirmation timestamps once. Preserve every message, source/location identity, received time, recipient, capture context, attachment, summary, queue, and suppression. Do not seed evidence from any existing proxy. Older binaries refuse the new schema. Add rollback injection proving table creation, stamp clearing, and version advancement roll back together. This additive migration does not rebuild `messages` or borrow the future M2.2 rebuild path.
3. Make `_record_locations` the single transactional source-observation owner. It receives the actual header-fetch signal, folder completeness, and observation time, persists source evidence when applicable, and computes the location stamp using D-identity. Migration observations carry no header evidence. Captures, including a copy that joins an existing logical message, carry fetched-header evidence. The public location-recording path resolves membership through the existing source-key owner, then calls this owner; remove its timezone query and retained-row branch. Require explicit `headers_observed` at that entry point and update every caller.
4. Preserve evidence through logical canonical promotion and coalescing by updating its logical association alongside locations using the existing transitions. Keep source keys isolated across copies, accounts, providers, and mailbox epochs. An unknown source or a rejected/suppressed capture must not mint evidence. A source with unresolved legacy identity cannot acquire an invented current identity; use the existing effective-identity helper and continuity rules.
5. Extend logical-unit deletion cleanup for `delete_message`, `clear_messages`, and purge. Remove evidence with the unit so recapture starts with its own observation. Folder-stamp invalidation retains header evidence; retain the current transaction boundaries and serialize writes with the same immediate transaction as location recording. A rollback must leave evidence, recipients, and locations unchanged together.
6. Adjust documentation references and tests around future schema numbers as part of this accepted prerequisite. Do not implement derived caches or the messages CHECK rebuild in this slice.

## Verification plan

Fail-before probes already failed on the named main head: three assertions, covering empty/nonempty fetched recipients and an unfetched duplicate borrowing canonical context. Commit those minimal inputs as regression tests and show pass-after on the implementation head.

Boundary probes and sibling cases:

- Fresh captures with and without capture timezone and with empty/nonempty recipients; retained copies before and after fetch, including reopening.
- Two source copies of one logical message: fetched canonical/unfetched duplicate and the reverse; multiple providers, accounts, and mailbox epochs with matching provider ids.
- A header fetch with incomplete scope, no admitted folder, and empty To/Cc; later complete observation. Partial, truncated, and out-of-scope folder observations still invalidate confirmation; a later complete observation can restore it using that source's evidence.
- Polling-gap invalidation, account replacement, legacy continuity binding and canonical promotion; no evidence transfer between source keys.
- Suppressed/unknown sources, deleted units, clear, purge, and recapture; no orphan rows or resurrection from stale evidence.
- Schema-29 and schema-30 upgrades, fresh schema-31 initialization, repeated opens, rollback injection and older-version rejection. Unchanged payload values and no provider calls during migration.
- Concurrent header and folder observations under both transaction orders: no lost header fact, and the final stamp follows the last folder observation rather than thread scheduling.
- Real service known-message paths preserve the fetch budget; source evidence controls the database stamp, not a downstream repair.

Run targeted regression and adjacent DB/service tests, Ruff, and the thread-view seam tests first. Because this changes storage and migration, run the repository's required local migration/coverage gates before the implementation push. CI owns the required platform jobs. Review and merge remain pinned to the tested, reviewed exact head with no unresolved threads.

## Implementation summary

This PR contains the proposed owning definition, prerequisite plan, and future schema-reference changes only. Runtime code and repository regression tests are not yet modified. Private evidence is durable outside the worktree and can be cited by alias and SHA256 in the PR.

## Cold diff audit

- `docs/THREAD_VIEW_CONTRACT.md`, D-identity: replaces the ambiguous observation rule at its one owner.
- `plans/PR-Thread-M2-Capture-Reconcile-Bodies.md`, steps 5/6 and schema references: points to the owner and prerequisite; keeps the accepted rebuild guarantees while moving their version names together.
- This plan: names the reproduced origin, required storage change, lifecycle writers, explicit non-scope, and fail-before/pass-after verification.

## Gap audit

NOT DONE

Implementation, passing regression evidence, migration qualification, and exact-head implementation review remain pending acceptance of this plan. The plan does not claim that M2.3 discovery or source evidence has shipped.
