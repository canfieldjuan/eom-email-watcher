# Windows admission timeout — contract-first origin repair

Status: CONTRACT PROPOSED. No implementation or new guest execution authorized by this revision until independent review and Juan's exact-revision acceptance.

## Authority and acceptance boundary

Oversight relayed Juan's exact decision on 2026-10-30 in mailbox ae860166: **“I accept the repair decision”**. That authorizes preparing this contract first, as a separate commit, followed by review and exact-revision acceptance before implementation. It does not accept this unwritten revision retroactively. PR #311 remains a diagnostic candidate; its permanent merge is not authorized.

Work from main 3edcae19de0fff45ebc808aa5650e17a411a2e45. Keep this repair separate from the diagnostic PR. Do not merge diagnostic commits into main as an incidental dependency.

## Root cause: confirmed boundary and unresolved origin

The disposable Windows desktop reproduced its manual-repair refusal while its original engine, desktop and configuration hashes remained unchanged. The labelled diagnostic at a989ee57083a5de78d339e56b1059f2b1c0237d4 recorded:

`configuration admission failed at refresh_revalidation (engine_timeout)`

Oversight b5ba059e passed the evidence, not the underlying behavior. The source path is AdmissionCoordinator::refresh_with -> AdmissionWorkers::revalidate -> Engine::compare_admission (`desktop/src-tauri/src/engine.rs:2629`), which calls config.admission.compare with DISCLOSURE_REQUEST_TIMEOUT (`engine.rs:45`, five seconds). The timeout can originate while waiting for primary child exit (`request_inner`, :3169) or during output collection (:3191). These engine line anchors are shared by main and the diagnostic head.

The existing refresh/refusal owner and deadline were introduced by 153e0d4bbf8fbc017f6e88a516f38d7ee4fa316a. That is not proof it introduced the excessive latency. The first component responsible for the delay, and its introducing commit, remain UNKNOWN. Resource competition is a hypothesis, not a finding. No behavior change can be chosen until the measurements distinguish the phases and a minimal reproduction isolates the origin.

## Validated work and reuse inventory

All aliases below are retained under the durable `145-poll-backlog/installed-runs` evidence packet; permissions remain private. PR evidence references use alias and digest, never private filesystem names.

| Existing proof | Pin / evidence | Reuse and limit |
| --- | --- | --- |
| Installed guest diagnostic | diagnostic-guest-receipt.json SHA-256 a9fd18faba882b9125b8413f236e88d5a456184378e0633f8a8dd383e8ee36d4; candidate a989ee57083a5de78d339e56b1059f2b1c0237d4 | Retains original binaries/config, interactive proofuser launch, stderr, screenshot, process identity and shutdown proof. Only stage/code attribution. |
| Raw diagnostic | diagnostic-a989ee5.stderr.log SHA-256 9bff1b03d92d249b2661d421ce749f816316e59154db89c8a4ead730e402d4e7 | Reproduces the exact refusal and later cancellation; no phase duration. |
| Serial admission control | admission-control-tokens.json SHA-256 2a5ebb0339aaea4fe82e820ea1c5d3c2960972a6e6b3600b4e074c946546c0f1 | Same installed engine/config; snapshot/compare/snapshot accepted identical tokens. Serial success is not evidence for competing desktop work. |
| Native Windows packaging | CI 41005519225, all required checks passed; CI merge 873c7bf5a2ac3367e6e733185516418e48f40644 has candidate tree 62024db7d7139f8e5b2d90c727f540aeb9fd81bd | Reuse digest, tree-identity, installer/desktop parity verification. A new instrumented candidate needs its own build and exact-head gates. |
| Process/SQLite observer | Existing poll_observer.py, native Windows identity adapter and retained observer receipts | Reuse PID plus creation identity if needed. Keep database mode=ro/query_only, read authorizer, WAL, timeout zero and per-snapshot close. No healthy-app non-interference or timing PASS was established. |
| Child lifecycle regressions | engine.rs tests for bounded output, timeout/cancellation after primary exit, native Windows job descendant termination, admission mismatch and bound worker requests | Extend the actual owner regression for the proven class. Do not duplicate its rule, replace assertions or relabel source tests as installed proof. |

No model invocation, prompt, inference runtime or model parameter is involved in this admission experiment. No mailbox/provider/calendar operation is deliberately triggered. Preserve the existing synthetic profile and fixture configuration; no personal data or credentials are copied.

## Required change surface and measurement contract

First implement only the accepted diagnostic phase, then isolate the origin before any behavior fix. Measure at the existing owner in `desktop/src-tauri/src/engine.rs`: Engine::request_inner, EngineChild::spawn/collect_output and EnginePipeDrain::receive as needed to observe their existing boundaries. Caller attribution may use the existing admission decision owners in `lib.rs`. No alternative IPC, admission or scheduling rule is introduced.

For each observed admission comparison, retain a local numeric correlation id, monotonic timestamps, terminal phase/outcome, and durations for: request preparation; native spawn and pipe-drain setup; stdin dispatch; primary-child wait; stdout/stderr completion; and teardown. Record the actual effective deadline and its origin (request or enclosing operation). A timed-out phase is incomplete, not a successful duration; later unreached phases are absent, not zero.

The deadline currently starts after stdin dispatch. Preserve that location and every existing deadline, cancellation and result mapping while measuring. Pipe drains begin during spawn and run concurrently with child execution: distinguish their completion times from the later collection wait. Do not sum overlapping durations or label wall time as CPU time. Capture the actual terminal site before cleanup so cleanup cost cannot masquerade as the original timeout.

Use fixed phase/operation enums and bounded numeric metadata. Do not record request/response bodies, admission tokens, paths, configuration values, error.message or command lines. Any error code uses the already reviewed closed identifier rule; no second sanitizer. Preserve results, fail-closed transitions, pipe limits, process-tree ownership and output collection semantics. Instrumentation must have an explicit diagnostic-only activation and exact build receipt; it must not silently become a general product telemetry feature.

Use read-only native OS observations keyed by PID plus creation identity for CPU time, child/process overlap, available RAM and I/O counters, with collection bounded to the comparison window. No fabricated load, additional parallel calls, process termination, cache flush, antivirus exclusion, priority change or resource eviction. Counter correlation alone cannot prove causation. Distinguish primary process, packaged child descendants and other work where the native APIs allow it; record unavailable data honestly.

All temporary measurement code and control commands must be retained with the evidence. Reuse the validated guest/observer and child-lifecycle owners; any necessary bounded observer addition is a measurement-only delta with boundary tests and a non-perturbation control. Do not change observer storage semantics.

## Explicit differences from the existing proof

1. Add separate phase durations and completion outcomes to the stage/code-only diagnostic; this supplies the missing attribution. No admission policy changes.
2. Add native resource counters to the existing process-identity evidence; this can distinguish waiting from CPU/I/O activity but is not permission to infer competition without a controlled comparison.
3. Compare ordinary desktop startup with the retained serial control under the same unchanged guest/profile/engine and resource gates. Record cache state as unknown unless directly established; do not flush caches or call a repeat a cold start.
4. Reuse #311 diagnostics only in a separately labelled measurement candidate if needed. Record every source and binary difference. This does not authorize merging #311 or shipping its diagnostics through the repair.
5. A final product repair is a new candidate. Re-run the original failure and regression on that exact candidate; do not transfer diagnostic-only results to it.

No other departure is accepted by this contract. A required engine-package rebuild, new permanent diagnostic API or different root owner must be explained in a contract amendment before implementation.

## Origin repair and regression gate

After measurement, record the exact input, candidate, observed wrong output, earliest divergent component and introducing commit using blame/history and a before/after comparison. Isolate a minimal public fixture at the actual owner. Preserve the original native failing receipt even if reproduction is intermittent; do not call a non-reproduction a pass.

Before editing behavior, write the concrete origin explanation and planned regression into the contract. The accepted surface permits a narrow correction in the measured desktop IPC/admission owner only when it preserves the current product policy. If the fault originates in Python startup, packaging, the OS, another owner, or the deadline policy, stop behavioral edits and present the necessary amendment for exact-revision acceptance. Do not compensate downstream.

The regression must fail first for the actual defect, then pass with the origin fix and fail when that fix is removed. Retain valid fast comparisons, stale/mismatched tokens, real timeout, cancellation before/after primary exit, inherited pipe holders, bounded output, identity change and concurrent work as applicable. Keep original guard assertions intact. Search every reader/copy of the proven concept, fix its single owner and remove redundant special cases. One grouped implementation push per completed repair round.

NO timeout increase and NO new retries are authorized. If evidence proves the five-second deadline itself is wrong, explain the demonstrated cost, why an origin fix cannot satisfy the existing policy, and the safety implications in a separately justified contract amendment. Measurements alone do not authorize changing it. No caching past an identity change, parallelism, cap shrink, assertion weakening or skipped failure.

## Native execution and safety boundaries

Only the disposable win11-connect-proof guest and proofuser profile may run. Before each boot require MemAvailable >= 32768 MiB for its 16 GiB allocation, Linux proof guest off, sufficient disk, verified exact-head artifact integrity and package parity, and independent review plus all required CI checks. Do not displace workloads to meet the gate. Keep live app/settings/services untouched.

Preserve the original installed engine, desktop and configuration. Verify hashes before/after. Use separately labelled diagnostic binaries and ordinary interactive launch; no engine override, installer bypass or live promotion. Retain the original package receipts. Bound the first isolation session to one guest boot, at most three ordinary-startup/serial-control pairs and forty minutes; stop on a new failure reason, changed config/binary identity, unavailable measurement or failed resource gate, preserve evidence and classify before more runs. A subsequent session requires a recorded justification within the same accepted scope, not an automatic repeat.

Shutdown gracefully after evidence collection and verify both proof guests off. No recurring execution is authorized by this contract. Private evidence stays outside worktrees with directory mode 700 and file mode 600. Do not publish secret or private-data content.

## Verification plan and assertion-to-contract map

- Phase attribution: synthetic slow-child versus exited-child/held-pipe twins identify the correct terminal phase while preserving the same refusal; successful control completes. Exercise cancellation and unavailable timing without converting them to success.
- Diagnostic privacy: invalid/mixed/control/oversized metadata cannot leak paths, payloads or messages; valid fixed identifiers and finite bounded durations remain useful. Downstream logs consume only sanitized metadata.
- Timing integrity: preserve effective deadline selection, start location, overlapping drain semantics and process creation identity; boundary tests cover completion around the cutoff, stale PID reuse and missing observations.
- Root regression: exact minimal public defect fixture fails before the origin fix, passes afterward, and detects its removal. Each changed assertion maps to the named invariant above or the measured root defect; none merely blesses new implementation output.
- Adjacent evidence: existing admission, child lifecycle, output/cancellation and source-order tests plus formatting and Clippy. Required CI owns duplicated broad suites; run platform-specific native tests where needed. Do not rerun code suites for this contract-only commit.
- Installed proof: exact candidate reproduces then corrects the original ordinary-launch failure while stale/unsafe inputs still refuse. A pass requires verified original engine/config identity, correct phase attribution and no weakened deadline. Resource-limited non-reproduction stays inconclusive.

## Implementation summary and cold diff audit

This revision adds this contract only. No source, test, dependency, workflow, configuration or generated file changes. No guest run is performed for contract preparation. The planned measurement, native controls, owner repair and final qualification remain pending. Review the contract against the retained evidence, not against an inferred timeout cause.

## Gap audit

NOT DONE: independent contract review and Juan's exact-revision acceptance; phase measurement; origin/introducing-commit isolation; accepted concrete repair; fail-first regression; exact-head CI/review; native proof; normal merge gates. #311 permanent merge, live installation and settings changes remain outside authority.
