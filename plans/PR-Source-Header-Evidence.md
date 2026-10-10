# Source-header evidence prerequisite (F3)

Status: ACCEPTED. The operator accepted this plan; plan PR #229 merged as `b24e099c4d9c1cd237affa2712c71d28f853bc8c`. Implementation follows in a separate PR.

## Contract

### Root cause

At main `4a375a28c8ae5797cf0a30f3d76121ecd9b4fa92`, `Store.record_message_location` (`src/eom_email_watcher/db.py:9081-9088`) reads the canonical message's `capture_timezone` to decide whether a source's headers were fetched. Commit `4d3e44c0` introduced this proxy. Capture date context belongs to the logical message's historical capture; source header observations belong to an individual mailbox copy. Neither a retained message's later metadata fetch nor an empty To/Cc result updates this proxy. A different copy can also inherit the canonical copy's context despite its own headers remaining unfetched.

The isolated production-store probes reproduce both errors: a retained source fetched with empty or nonempty To is stamped, survives reopening, then loses its stamp on a complete folder-only observation; an unfetched duplicate receives a stamp after its canonical row is assigned capture context. These are prerequisites for reliable M2.3 discovery, not evidence of a current discovery implementation.

The owning product definition is [D-identity, recorded locations](../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys). Implementation must consume it without restating a separate rule.

### Required change surface

- `db.py`: schema-31 migration, one source observation owner with persisted folder ordering, all capture and observation writers, one shared logical re-parent helper, and logical-unit deletion.
- `service.py`: header-bearing fetch paths consume the existing response-scope decision before storing recipients or header evidence; folder-only observations remain distinct from fetch responses.
- `tests/test_db.py`: port the minimal probes into repository tests and cover migration, source isolation, lifecycle, rollback, and concurrent observation order.
- `tests/test_service.py`: exercise real known-message folder observations against stored source evidence; verify no metadata request is introduced by polling.
- Thread-view contract: amend only the recorded-locations definition. M2 plan: reference this prerequisite and move its future schema numbers and backup names together.
- No desktop, provider transport, model, or public engine protocol change is required. The internal `record_message_location` header-evidence argument becomes required so callers explicitly identify whether a header fetch occurred.

### Explicit non-scope

No timezone backfill, recipient replacement, extra provider fetch, admission change, folder-history removal, body storage, vendor matching, coverage generation, claim extraction, dependency change, or unrelated refactor. F5 Microsoft routing and M2.2 implementation remain separate. Existing scheduling, analysis, notifications, cutoff rules, and Connect decisions retain their behavior.

### Assumptions and blockers

Schema 30 has no reliable independent source-header fact. Existing confirmation timestamps can contain the demonstrated false positive; recipient rows and capture context cannot distinguish every fetched copy. The migration therefore conservatively discards confirmation once and reconstructs no header evidence. This can increase future discovery fetches within its existing bounds, but migration itself performs no network or model calls.

Schema 31 is currently unimplemented but reserved by the accepted M2 plan. This prerequisite consumes 31; M2.2, M2.3, and M2.4 become 32, 33, and 34. M2.2's messages-rebuild backup becomes `pre-v32`, with source version 31. This renumbering changes no accepted milestone behavior or backup guarantees. The operator accepted this amendment before implementation.

## Implementation plan

1. Add `message_source_observations(message_id, provider, account_id, mailbox_identity_key, provider_message_id, headers_observed_at, folder_observed_at, folder_complete)` in schema 31. The source tuple is the primary key; `message_id` associates the row with its logical unit. `headers_observed_at` is nullable, independently recording accepted fetched headers. `folder_observed_at` is a nonempty canonical UTC timestamp and `folder_complete` a constrained boolean. This row also exists for folder-only observations when header evidence is unknown. Empty recipients are valid for an accepted in-scope response; an out-of-scope fetch is handled by D-scope, not interpreted as an empty header result.
2. In the migration transaction, create the table and its logical-message index and deletion cleanup before shared logical transitions need them on an upgrade; clear existing location confirmation timestamps once. Seed each known source with unknown header evidence and an incomplete folder observation at the migration's frozen UTC time, so an older delayed observation cannot undo invalidation. Resolve source identity through the existing effective-identity helper and source membership owner; do not invent an identity for an unresolved legacy row. Preserve every message, source/location identity, received time, recipient, capture context, attachment, summary, queue, and suppression. Older binaries refuse the new schema. Add rollback injection proving table creation, stamp clearing, and version advancement roll back together. This additive migration does not rebuild `messages` or borrow the future M2.2 rebuild path.
3. Make `_record_locations` the single transactional source-observation owner. It receives the explicit header-fetch signal, folder completeness, and UTC observation time captured before waiting for the database. Header-bearing callers first consume the existing response-scope decision, storing accepted recipients and header evidence in the same transaction. A last-line store guard rejects a header-bearing observation with no admitted location, without mutation; it consumes the caller's validated locations rather than re-parsing a provider response. Folder-only observations with no locations can still invalidate a known source. Migration observations carry no header evidence. Accepted captures, including a copy that joins an existing logical message, carry fetched-header evidence. The public location-recording path resolves membership through the existing source-key owner, then calls this owner; remove its timezone query and retained-row branch. Require explicit `headers_observed` at that entry point and update every caller.
4. In that owner, compare the incoming folder time with the stored time using exact UTC datetime precision, not a lossy SQLite floating-point epoch. A newer observation replaces folder completeness, an older one cannot replace the selected folder state, and equal times combine completeness conservatively. Accepted header evidence is independent and monotonic: an older accepted fetch may establish that fact without replacing a newer folder state. Compute the source's location stamps from the selected stored folder state and header fact under D-identity; never stamp with the late writer's time. Historical admitted locations remain a union, preserving their existing meaning. A polling gap records an incomplete folder observation at the gap's observation time through this same owner, so a delayed pre-gap complete result cannot restore confirmation. Capture, direct observation, migration and account-gap paths must all use the owner; there is no parallel timestamp update path.
5. Add one shared logical re-parent helper called by both startup coalescing (`_migrate_sent_capture`) and runtime canonical promotion (`_bind_pending_legacy_identity`). It moves logical ownership of locations and source observations together and merges recipient sets through the existing recipient owner. It never changes source keys. Remove the duplicated re-parent sequences from those callers. Per-row historical summaries, attachments, and automation runs stay on their original storage rows as the accepted M2 plan requires; do not indiscriminately move every table with a `message_id` column. Cover both transitions, including a copy with no row of its own, and prove all source evidence stays attached to the logical unit after promotion. A source with unresolved legacy identity follows the existing continuity rules.
6. Extend logical-unit deletion cleanup for `delete_message`, `clear_messages`, and purge. Remove observations with the unit so recapture starts with its own observation. Invalidations retain header evidence; retain the current immediate transaction boundaries. Rollback must leave observations, recipients, and locations unchanged together. Unknown sources and rejected or suppressed captures must not mint observations or evidence.
7. Adjust documentation references and tests around future schema numbers as part of this accepted prerequisite. Do not implement derived caches or the messages CHECK rebuild in this slice.

## Verification plan

Fail-before probes already failed on the named main head: three assertions, covering empty/nonempty fetched recipients and an unfetched duplicate borrowing canonical context. Commit those minimal inputs as regression tests and show pass-after on the implementation head.

Boundary probes and sibling cases:

- Fresh captures with and without capture timezone and with empty/nonempty recipients; retained copies before and after fetch, including reopening.
- Two source copies of one logical message: fetched canonical/unfetched duplicate and the reverse; multiple providers, accounts, and mailbox epochs with matching provider ids.
- An accepted in-scope header fetch with incomplete scope and empty To/Cc, then a complete folder observation. An out-of-scope fetch stores neither recipients nor header evidence nor a folder observation; a later folder-only event on an unfetched source remains unconfirmed until an accepted fetch. Partial, truncated, and out-of-scope folder-only observations still invalidate confirmation under D-identity.
- Polling-gap invalidation, account replacement, legacy continuity binding, canonical promotion and startup coalescing through the shared helper; no evidence transfer between source keys and no orphan logical association.
- Suppressed/unknown sources, deleted units, clear, purge, and recapture; no orphan rows or resurrection from stale evidence.
- Schema-29 and schema-30 upgrades, fresh schema-31 initialization, repeated opens, rollback injection and older-version rejection. Unchanged payload values and no provider calls during migration.
- Concurrent complete-at-t1 and incomplete-at-t2 observations (t2 > t1), then the reverse completeness pattern, under both commit orders. The selected time and final confirmation must agree in both orders. Equal-time mixed observations are conservative in both orders; accepted header evidence is not lost. A delayed pre-gap result cannot undo a newer invalidation. Use real Store connections and barriers, including subsecond time boundaries, not a mock fold.
- Real service known-message paths preserve the fetch budget; source evidence controls the database stamp, not a downstream repair.

Run targeted regression and adjacent DB/service tests, Ruff, and the thread-view seam tests first. Because this changes storage and migration, run the repository's required local migration/coverage gates before the implementation push. CI owns the required platform jobs. Review and merge remain pinned to the tested, reviewed exact head with no unresolved threads.

## Implementation summary

Plan PR #229 contained the owning definition, prerequisite plan, and future schema-reference changes only. Its diff did not modify runtime code or repository regression tests. Private evidence is durable outside the worktree and can be cited by alias and SHA256 in the PR.

## Cold diff audit

- `docs/THREAD_VIEW_CONTRACT.md`, D-identity: replaces the ambiguous observation rule at its one owner, including event ordering and a reference to the existing response-scope owner.
- `plans/PR-Thread-M2-Capture-Reconcile-Bodies.md`, steps 5/6 and schema references: points to the owner and prerequisite; keeps the accepted rebuild guarantees while moving their version names together.
- This plan: names the reproduced origin, required storage change, lifecycle writers, explicit non-scope, and fail-before/pass-after verification.

## Gap audit

NOT DONE

Plan PR #229 did not implement or qualify the runtime change. Implementation evidence and exact-head implementation review belong to the separate implementation PR. The plan does not claim that M2.3 discovery or source evidence has shipped.
