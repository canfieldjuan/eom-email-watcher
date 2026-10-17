# Separate COI guard diagnostic — accepted contract

## Root cause
The existing evidence-directory guard (introduced by 8d832259) omits the Git ancestor responsible for refusal. The failed full gate could therefore identify the owner but could not preserve the triggering marker. The intermittent rejection remains unexplained.

## Required change surface
Only scripts/coi_evidence.py::evidence_directory and its existing Git-destination negative test in tests/test_coi_local_proof.py. The accepted patch names the marker that triggered the existing refusal. Publish separately from the frozen Windows privacy implementation.

## Explicit non-scope
Preserve the guard, resolved path, iteration order, Git marker file/directory and symlink treatment, all existing assertions, Windows privacy scope, paths, deadlines and schemas. No new harness, skip, retry, temp-directory policy or bypass.

## Assumptions and blockers
This helper is outside the accepted #269 surface. The original goal prohibits extra repo diagnostics beyond accepted tests. Direct operator authorization received: "Authorize separate diagnostic (Recommended)". This accepts the concrete proposal and authorizes its separate implementation and publication. This supplies evidence for a future rejection; the original rejection still needs an explanation before signoff.

## Verification plan
First add the exact error assertion to the existing negative test: it must fail because the current generic error lacks the ancestor. Apply the owner change, then run the existing inside/outside/symlink/marker controls and Ruff. Keep the original failure evidence. Full-suite baseline and isolated-candidate comparisons already running have no production edits.

## Implementation summary
scripts/coi_evidence.py::evidence_directory now raises the same RuntimeError with the existing prefix plus the first rejecting Git marker. tests/test_coi_local_proof.py adds one exact-message assertion to the existing marker-file/directory and direct/symlink controls; the original refusal and no-creation assertions remain. No duplicate guard or downstream special case was introduced; none existed to remove.

## Cold diff audit
scripts/coi_evidence.py:14-23: explicit loop preserves resolve(), nearest-first ancestors and Path.exists(); only the refusal diagnostic changes. This satisfies the diagnostic-owner contract. tests/test_coi_local_proof.py:454-478: capture the existing RuntimeError and assert its exact marker; external acceptance/no-creation controls remain. plans/PR-COI-Guard-Diagnostic.md records direct acceptance and verification.

Reproduce/isolate/explain: at acceptance commit 954bbfd, the existing guard introduced by 8d832259 refused each synthetic Git destination but omitted its marker. The new exact assertion failed in all four existing cases with AssertionError (4 failed in 0.75s). Fix/prove/prevent: the owner loop supplies that marker; the unchanged adjacent module plus import-context tests pass (81 passed in 7.52s). uv run ruff check . and git diff --check pass. All commands used the repository environment under umask 022.

boundary-probe: Git file and directory, direct and symlinked destinations refuse and name the resolved marker; an external destination returns unchanged without creation. These are scalar paths, so mixed-key/default-count boundaries do not apply.

effect-trace: name the rejecting Git ancestor | evidence_directory owns resolve/ancestor traversal and the RuntimeError | the exact-message assertion fails before and passes after the owner edit.

Evidence aliases and SHA256 (durable operator evidence; no private content):
- coi-diagnostic-fail-first.log: 8d1d23d4f434bd2142bc416c27a7f25932b46befa629dc9e3b7fed5d170da398
- coi-diagnostic-adjacent.log: fd87af942fe35ac5e093baad16fc13c946572acf5b3cb34d072592936e8fd18e


## Gap audit
NOT DONE

The separate diagnostic is implemented and locally verified. Publication, exact-head CI/review and merge remain pending. It does not explain or clear the earlier intermittent full-suite rejection; Windows privacy remains held. No broad Python rerun duplicates required CI for this diagnostic-only change.

Accepted proposal patch SHA256: c9d66f147db90240f523ea86c40439e74263761b1f643239de4749ef245c611a
