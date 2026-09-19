# Local Provider Credential Flow

Status: design, resolving the open item that blocks the local EOM Connect
provider slice.

This document defines how a downloadable, per-PC automation reaches the EOM
funnel capabilities without embedding a shared, tenant-wide credential on any
buyer PC. It is grounded in the code as it stands today, not on the discovery
doc alone. Citations are to the four repos on disk at the revisions read for
this design.

## The problem, stated against the real code

The EOM funnel API on Atlas authenticates its caller with a single shared
symmetric bearer and nothing more:

- `atlas/atlas_brain/eom_api/funnel_auth.py:211-228` `require_eom_funnel_api`
  does a constant-time SHA-256 compare of one configured digest
  (`service_token_sha256`). There is exactly one valid token. Atlas stores only
  its digest; the raw token is rejected on the Atlas side
  (`atlas/atlas_brain/eom_api/config.py` service-token validator).
- `atlas/atlas_brain/eom_api/funnel_auth.py:231-247` `require_eom_funnel_actor`
  reads `X-EOM-Actor` and `X-EOM-Actor-ID` with no cryptographic check. The
  actor is trusted precisely because the service bearer already authenticated
  the caller. The comment says so: "Accept actor evidence only after the
  dedicated service is authenticated."

The consequence: whoever holds the funnel bearer can assert any operator
identity and mutate any EOM contact. That power is correct for exactly one
hardened backend. It is catastrophic on N buyer PCs, where the token can be
extracted, cannot be revoked per PC, and rotates only by re-provisioning every
machine at once.

Today that one hardened backend is the tracker, and it already does the right
thing:

- `eom-timetracker/backend/time_tracker_api.py:4951-4954` reads
  `ATLAS_FUNNEL_SERVICE_TOKEN` from the environment.
- `eom-timetracker/backend/time_tracker_api.py:5238-5267` `_atlas_funnel_request`
  sets `Authorization: Bearer <token>`, `X-EOM-Actor`, `X-EOM-Actor-ID`, and
  `Idempotency-Key`, and guards the path to `/eom-funnel/`. The comment: "Call
  Atlas from the server; its service credential never reaches a browser."
- The tracker owns the write-authority ledger and the pending-resume
  discipline: `eom_office_conversion_handoffs` and
  `eom_customer_atlas_reservations` are written and committed before Atlas is
  called (`time_tracker_api.py:7090-7119`), and the finalize path returns HTTP
  202 `atlas_pending` on an uncertain Atlas result and reconciles on replay via
  the reservation id doubling as the Atlas `sourceRef`
  (`time_tracker_api.py:24781-24844`).

So the tracker is not merely a bearer holder. It is the single reservation and
write authority in front of Atlas, and it already vouches for many operators
under one service bearer through the actor headers.

## The decision this forces

A buyer PC that called Atlas directly would have to carry an Atlas-reaching
credential and would sit on the wrong side of the tracker's reservation ledger.
It would either duplicate `eom_office_conversion_handoffs` and the 202-pending
resume onto every PC, or bypass them and lose the single write authority. Both
are wrong.

Therefore the local provider does not authenticate to Atlas at all. It
authenticates to the tracker as a device-bound operator principal, and the
tracker keeps doing exactly what it does today: hold the Atlas bearer
server-side, own the reservation ledger, and stamp the bound operator as the
actor. The buyer PC never holds an Atlas credential. What it needs is a
hardened, revocable, unattended device identity to the tracker, in place of the
12-hour interactive browser session the portal uses today
(`time_tracker_api.py:4805,4812` `JWT_SECRET`, `TOKEN_TTL_HOURS`).

This is both the correct path and, as it happens, the lighter one, because the
tracker already solved bearer custody, the reservation ledger, idempotency, the
202-pending resume, and actor vouching. The work here is the device
authentication, not a second copy of any of that.

### Topology

```
Automate host (consumer)                buyer PC
   |  Connect v2, same-PC loopback (ADR-0005)
   v
Local EOM Connect provider (new)        buyer PC
   |  HTTPS, device-bound operator proof
   v
Tracker office API (existing authority) Render
   |  Authorization: Bearer <one service token>, X-EOM-Actor headers
   v
Atlas funnel API                        Render
```

The Connect boundary (consumer to local provider) stays exactly as ADR-0005 and
ADR-0006 define it: same-PC loopback, `%LOCALAPPDATA%\LocalConnect` on Windows
or the XDG path on Unix, per-user DACL, one admission lane, and the
`connect.automations` entitlement gating that automations run at all. That
boundary is unchanged and out of scope here. This document is only about the
provider-to-cloud hop, which Connect does not govern (ADR-0005 places app
databases and each app's own cloud outside the interop contract).

## Rejected alternative: Atlas verifies signed grants directly

The considered alternative is to extend Atlas with an asymmetric verifier so the
local provider calls Atlas directly with a short-lived, scoped, Ed25519-signed
grant, and Atlas derives the actor from the signed claims instead of trusting
headers. Atlas has no such verifier today; its only asymmetric primitive is the
Connect entitlement license verify in this repo
(`src/eom_email_watcher/entitlement.py:432`), and its funnel path is
SHA-256-only. Building the verifier is feasible and would cryptographically bind
the actor, which is genuinely stronger than today's trusted headers.

It is rejected because it puts the credential on the wrong boundary. The actor
binding it buys can be obtained at the tracker instead (see below), which is the
boundary that already holds the reservation ledger. Verifying at Atlas does not
move the ledger, so a direct-to-Atlas provider still has to either duplicate
`eom_office_conversion_handoffs` and the 202-pending resume onto the buyer PC or
bypass them. It also forks the actor-vouching trust into a second place, and it
requires a net-new Atlas verifier plus an online revocation store plus a grant
refresh endpoint, which is more surface for a weaker overall result. The one
advantage, a cryptographic actor at Atlas, is delivered in the chosen design at
the tracker, which is where the write authority already lives.

## Chosen design

Two separate credential concerns, kept distinct because they have different
authorities and different revocation stories:

1. Pack and automation entitlement. Release-time, machine-agnostic, offline
   signed. This already exists as the Ed25519 entitlement license
   (`entitlement.py`, verified against a compiled public keyring with additive
   `key_id` rotation) and the pack grant added in Slice 4. It licenses that
   automations may run on this PC and that this pack is licensed. It is issued
   by an offline release authority (`connect-contracts/tools/entitlement_issuer.py`)
   and, per ADR-0003, is deliberately not machine-bound and has no online
   revocation. It does not authenticate the operator and does not reach the
   tracker. It is unchanged here.

2. Device-bound operator authentication to the tracker. Online, per-customer,
   revocable. This is the new artifact and the subject of the rest of this
   document. Do not conflate it with (1): the offline release issuer must never
   become an online per-device authority, and the device identity must never be
   used to license packs.

### Device identity: a locally generated keypair

At enrollment the buyer PC generates its own Ed25519 keypair. The private key
never leaves the PC. It is stored owner-only under the Local Connect data root
(`%LOCALAPPDATA%\LocalConnect\device\` on Windows with the per-user DACL from
ADR-0005; `$XDG_CONFIG_HOME/local-connect/device/` at mode 0600 on Unix), using
the same ownership and permission discipline the entitlement loader already
enforces (`entitlement.py` owner-only path checks; the issuer's
`_read_regular_file` private-file checks). The tracker only ever receives the
public key. No shared secret is transmitted to or stored on the PC.

The device private key is the durable device identity. Possession of it, and
nothing copyable off the wire, is what later proves the device.

### Enrollment (once per PC, operator-authenticated)

1. The operator signs into the tracker portal with their existing session
   (`get_current_employee`, `time_tracker_api.py:2417/2427`), the same
   credential the office already uses.
2. In the portal the operator chooses to link a device. The local provider
   submits its device public key and a proof of possession (a signature over a
   tracker-issued enrollment challenge) to a new tracker enrollment endpoint.
3. The tracker verifies the proof against the submitted public key and records a
   device row bound to that one operator:
   `{ device_id, employee_id, device_public_key, label, status, created_at,
   last_seen_at }`, with `status` drawn from a closed set
   `{ active, revoked }`. This mirrors the durable-status-row pattern the
   public-onboarding tokens already use
   (`atlas/atlas_brain/services/eom_public_onboarding_tokens.py` statuses
   `issued/redeemed/revoked`; store columns in `funnel_store.py`).

The device is now bound to exactly one operator. Binding a device to a second
operator is a second enrollment producing a second device row, never a mutation
of the first.

### Access (per operation, or per short device session)

1. The local provider proves possession of its device private key to a
   device-facing tracker endpoint by signing a fresh, short-lived,
   single-use challenge (a DPoP-style proof: method, path, a server nonce, and
   an expiry, signed by the device key). This defeats replay and does not put a
   bearer on the wire that is useful if captured.
2. The tracker verifies the signature against the stored device public key,
   checks `status = active`, and updates `last_seen_at`.
3. The tracker then performs the funnel operation through its existing
   `_atlas_funnel_request`, stamping the device's bound operator as
   `X-EOM-Actor` / `X-EOM-Actor-ID`. The operator identity presented to Atlas is
   now anchored to a verified device key and a recorded enrollment, which is
   strictly stronger than the portal's session bearer, and the anchoring happens
   at the tracker, the write-authority boundary.

Optionally the tracker may issue a short-lived device session token (minutes,
not the portal's 12 hours) after a proof, to amortize the proof over a burst of
reads. Any such token is bounded by a short TTL and is revoked in effect the
moment the device row flips to `revoked`, because issuance stops and the TTL
expires the residual. Writes should require a fresh per-operation proof
regardless, per the confirmation rule below.

### Scope and the confirmation gate

The device is authorized only for the intersection of what the pack licenses and
what the bound operator's role permits. The capability catalog and its risk
flags already exist in the provider manifest
(`connect-contracts/fixtures/v2/valid/manifest-eom-funnel-provider.json`): every
external mutation carries `effects.confirmation_required: true`, and reads carry
`false`.

The hardening rule the plan already states applies here without exception: a
capability whose flags mark it confirmation-required must not be dispatched from
an automatic trigger without a fresh, authorized confirmation linked to that
specific operation. So an unattended device session may run reads and
non-confirmation effects on its own, but a booking, handoff, or contact mutation
requires a per-operation operator confirmation carried as evidence to the
tracker, which the tracker binds to the operation before it calls Atlas. The
device proof authenticates the caller; it does not stand in for the operator's
confirmation of a specific external mutation. This is enforced at the tracker
admission path, not by copying the flag into a fixture.

### Rotation

- Device key rotation is a re-enrollment: the device generates a new keypair,
  the operator links it, and the old device row is revoked. No key material is
  ever updated in place on an existing row.
- The tracker's single Atlas service bearer rotates server-side exactly as
  today, by swapping the configured SHA-256 digest
  (`ATLAS_EOM_FUNNEL_SERVICE_TOKEN_SHA256` on Atlas,
  `ATLAS_FUNNEL_SERVICE_TOKEN` on the tracker). Devices never see it, so this
  rotation is invisible to every buyer PC.
- Any Ed25519 verification keys the tracker uses for enrollment challenges
  rotate additively by `key_id`, the same additive rotation the entitlement
  keyring uses (ADR-0003:90-94), and the public-onboarding primary-plus-previous
  secret precedent covers a dual-accept window during rotation
  (`config.py` `public_onboarding_hmac_secret` / `previous_hmac_secret`).

### Revocation

Revocation is a single durable-status flip on the tracker device row to
`revoked`. It is immediate for new access, because the tracker refuses to issue
any further proof result or session token to a revoked device, and the residual
of any short session token is bounded by its minutes-long TTL. This is the
online, per-PC revocation the entitlement license explicitly lacks (ADR-0003
defers online revocation and machine binding); it lives at the tracker because
that is the online authority, while the offline-signed entitlement stays offline
and machine-agnostic. Losing a laptop, offboarding an operator, or retiring a PC
is one row update, and it revokes that PC alone.

## Why this satisfies the constraint

- No shared, tenant-wide Atlas credential is ever placed on a buyer PC. The only
  Atlas-reaching credential stays on the tracker, where it already is.
- Every funnel mutation is bound to a verified device key and a recorded
  operator enrollment, which is stronger than today's trusted actor headers, and
  the binding is enforced at the write-authority boundary.
- Revocation is per PC and immediate. Rotation of the privileged downstream
  credential is invisible to PCs.
- The tracker's reservation ledger, idempotency, and 202-pending resume are
  reused unchanged, so buyer PCs never fork the single write authority.
- Atlas is not modified.

## Generalization to other providers

The same shape holds for any future provider-backed pack: the local provider
authenticates to the provider's existing cloud backend as a device-bound
principal, generating its keypair locally and enrolling its public key, and the
backend keeps its privileged downstream credentials server-side and remains the
single write authority. No pack, provider, or PC ever holds the backend's shared
secret. The EOM funnel is the first instance; the tracker is its backend.

## Build steps and where each change lands

Tracker (`eom-timetracker`), the online authority and the only new server
surface:

1. A device table and its closed status machine `{ active, revoked }`, modeled
   on the public-onboarding token store.
2. An operator-authenticated enrollment endpoint that verifies a proof of
   possession and records the device public key against `employee_id`.
3. A device-facing access endpoint that verifies a single-use, short-lived
   device proof and then calls the existing `_atlas_funnel_request` with the
   bound operator's actor headers. Optionally issue a short device session
   token.
4. The per-operation confirmation gate for confirmation-required capabilities,
   binding a fresh operator confirmation to the specific operation before the
   Atlas call.
5. Revocation as a status flip, plus refusal to issue further proofs or tokens
   to a revoked device.

Local EOM Connect provider (new component in the host ecosystem):

6. Device keypair generation and owner-only storage under the Local Connect data
   root, reusing the ownership and permission discipline from `entitlement.py`
   and the issuer's private-file checks.
7. The enrollment client and the per-operation proof client.
8. The Connect v2 provider surface that maps each funnel capability to a tracker
   call, minting a stable `job_id` per ADR-0002 and reconciling rather than
   resubmitting, so the tracker's 202-pending resume is honored end to end.

Atlas: no change.

Tests the provider slice must carry (extending the plan's provider-semantics
requirement): enrollment proof accept and reject, replayed-proof rejection,
revoked-device rejection, expired-session rejection, a confirmation-required
capability refused from an unattended trigger without a fresh confirmation, and
the tracker replay-versus-409 and 202-pending reconciliation exercised through
the provider so a wrong mapping fails a test.

## Open sub-item deliberately left to product

Whether enrollment requires a second factor beyond the operator's portal session
(for example an admin approval of each new device link) is a policy choice for
how tightly device provisioning is controlled. It does not change the mechanism
above and can be added as an approval step on the enrollment endpoint without
touching the device identity or the access path.
