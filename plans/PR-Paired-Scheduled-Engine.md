# Paired scheduled engine (Watcher #214)

## Validated inventory

Durable inventory: `paired-scheduled-engine-20261007/inventory.json`. It retains
`coi_local_proof.py`, the approved-native-coi digest, CUDA Qwen3.5-9B Q4_K_M,
pinned native runtime/template/request settings, matching certificate fields,
replay/restart checks and every source difference from installed revision83aa5cd.
Prior source parity is evidence for its recorded heads, not this deployment.
Installed consumer supports schema28; merged source supports schema30. No normal
DB migration is authorized before its desktop and scheduled readers are paired.

## Root cause

`packaging/engine_entry.py` packages only the API while
`scripts/install-user-services.sh` deploys an independent uv CLI snapshot;
`systemd/*.service` keeps using that snapshot after a desktop update.
Separate database owners produce a partial update and a newer unsupported DB.
The prior source-only local alignment did not remove this release defect.

## Required change surface

- One packaged entrypoint dispatches API or existing CLI with the same modules.
- Installed service CLI is an alias of the bundled engine, never another snapshot.
- Installer verifies the packaged dispatch before changing scheduled units/alias;
  an installed incompatible desktop engine fails closed instead of falling back.
- The bundled API entrypoint checks configured watcher/monthly service commands
  and active workers before request handling, hence before any Store migration.
  This is a last defense consuming installed command identity; it does not
  catch schema errors downstream or relax the DB reader.
- Remove checkout-dependent service working directories. Preserve timer cadence,
  sandbox, notification delivery, authority and confirmation behavior.
- One deployment owner validates all configured scheduled readers; docs reference it.

## Differences from proven setup

1. Both existing entrypoints share one packaged executable; source-only machines
   retain the locked uv installation when no desktop engine exists.
2. Bundled API startup refuses legacy/mixed readers and active scheduled workers
   before reading input. No service is silently cancelled or config migrated.
3. Scheduled services no longer require a development checkout as their cwd.
4. Candidate source uses merged schema30, whereas installed source is schema28;
   inventory lists every upstream difference. Pairing is proven on a public
   schema28 copy before normal deployment. Native settings remain unchanged.

## Explicit non-scope

No model/prompt/runtime, certificate schema, DB schema revision, confirmation,
timer cadence, mailbox delivery setting, unrelated UI or entitlement changes.
No normal DB rollback, historical retry migration or independent CLI update.

## Assumptions/blockers

Linux user services installed by this project are the deployment scope; Windows
has no such timer. Existing overrides must resolve to the paired executable.
Normal installation and real busy proof require reviewed source and compatible
artifacts. An active scheduled worker must finish before desktop migration.

## Verification plan

- Fail-first public subprocess: packaged entrypoint --cli --version must dispatch
  the CLI; existing API-only entrypoint returns invalid_json instead.
- Fail-first installer: incompatible discovered engine must reject before uv,
  service publication or any DB migration.
- Boundary probes: no services, compatible alias, mixed/legacy command, malformed
  unit metadata, missing executable and active worker; validate both services.
- Public schema28 copy: reject mismatch with unchanged digest/user_version; paired
  engine migrates it and the same binary CLI reads it successfully.
- Existing systemd authority/state-path and packaged smoke tests, Ruff/bash syntax.
- Build shared executable; exercise API and CLI from it; reuse approved COI native
  proof and field/replay/restart oracle after compatible installed deployment.

## Implementation summary / cold diff / gap audit

Pending. No code changed or normal profile touched at contract creation.
