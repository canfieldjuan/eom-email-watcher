# M3 Linux native reading proof

The combined implementation passed the native production-assets proof on Linux
on 2026-11-04. This is a local qualification, not an installation or deployment.

## Exact inputs

- Backend: PR #338, `67463a84fa1471f5f88cf8fd07346f6a160832eb`.
- Desktop: PR #339, `1283019a50079ccad030bfca46138a4c5c12f5bd`.
- Local combined implementation commit: `1605666f19f9bb0fc59fa6d7752bbd42fef7fa67`.
- Combined implementation tree: `3d779bba8f6a3f5e6911d00f3e6f8d9122513e39`.
  This equals `git merge-tree --write-tree 67463a8 1283019`, so it can be
  independently recomputed from the two published implementation heads.
- Local proof-source commit: `f77748f3e81ce657695cd530236bcf9ba8b72e40`.
- No local combined branch was pushed or used to merge a PR.
- The two probe source hashes in [result.json](result.json) match the scripts in
  this change. The result records host, sidecar and CSP configuration hashes too.

The backend and desktop field change must land together; each closed side rejects
its counterpart's old shape. Do not build or install between landing the pair.

## What passed

The actual packaged development sidecar, Rust host and production frontend served
through `tauri://localhost` read a newly seeded synthetic profile. The proof used
private Xvfb and D-Bus, an empty user-manager fixture, no provider credentials or
Connect entitlement, disabled notifications, and an unreachable local model.
No live configuration, mailbox, installed app or Windows host was exercised.

- Vendor entry and disconnected retained-account selection; the other account's
  thread disappears under the selected account filter.
- Thirty messages paged 25 + 5, with the newest subject as the heading even when
  its message is outside the first page.
- Real body, copy and attachment-inventory reads. Hostile strings remain literal
  text; partial, empty, pending, unavailable and unresolved-identity states render.
- A real IPC body response is held at Tauri's callback map. Collapsing before its
  release prevents repainting. The payload is not fabricated or changed.
- Native keyboard activation of the body button, back-focus restoration, empty
  vendor state and preservation of the full watched Inbox address.
- At 640 CSS pixels and 200% root text sizing, document scroll width stays 640;
  the body container spans x=33..607 with client/scroll width both 572. Capture
  waits for the actual viewport resize and two animation frames.
- Admitted startup, styles, navigation and a native dialog still work. The eight
  existing CSP probe classes are enforced; the local HTTP sink receives zero
  requests. No inline script or event handler executes.

Disconnected accounts report `sent_scope: not_polled` through the existing account
API; the proof checks that state and absence of a stale unavailable notice. The
positive `Sent mail unavailable` rendering case is covered by the DOM unit test,
not claimed as a connected-account native result here.

[640px body](thread-640.png) · [200% view](thread-640-text-200.png) ·
[200% body](thread-640-text-200-body.png)

## Reproduction

Use a disposable checkout containing both exact implementation inputs above plus
these proof scripts. Keep this combination local. Follow the repository's Linux
development sidecar and frontend prerequisites; use matching WebKitWebDriver,
Xvfb, D-Bus and xdotool. Unset OAuth-client and entitlement-keyring build inputs,
build the development sidecar, then:

```sh
pnpm --dir desktop build
cargo build --manifest-path desktop/src-tauri/Cargo.toml --features tauri/custom-protocol
.venv/bin/python scripts/prove_thread_csp.py \
  --binary desktop/src-tauri/target/debug/eom-email-watcher-desktop \
  --driver /absolute/path/to/WebKitWebDriver \
  --output /var/tmp/new-private-m3-proof-directory \
  --thread-view
```

The output directory must be new and outside Git. The helper intentionally uses
existing synthetic test fixtures, so the test sources must be present. The script
rejects optimized Python, preserves the original CSP assertions and cleans up its
private processes. This proof does not claim Windows or release qualification.
