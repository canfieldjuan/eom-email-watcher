# Separate COI guard diagnostic — accepted contract

## Root cause
The existing evidence-directory guard (introduced by 8d832259) omits the Git ancestor responsible for refusal. The failed full gate could therefore identify the owner but could not preserve the triggering marker. The intermittent rejection remains unexplained.

## Required change surface
Only scripts/coi_evidence.py::evidence_directory and its existing Git-destination negative test in tests/test_coi_local_proof.py. The attached unapplied patch names the marker that triggered the existing refusal. Publish separately from the frozen Windows privacy implementation.

## Explicit non-scope
Preserve the guard, resolved path, iteration order, Git marker file/directory and symlink treatment, all existing assertions, Windows privacy scope, paths, deadlines and schemas. No new harness, skip, retry, temp-directory policy or bypass.

## Assumptions and blockers
This helper is outside the accepted #269 surface. The original goal prohibits extra repo diagnostics beyond accepted tests. Direct operator authorization received: "Authorize separate diagnostic (Recommended)". This accepts the concrete proposal and authorizes its separate implementation and publication. This supplies evidence for a future rejection; the original rejection still needs an explanation before signoff.

## Verification plan
First add the exact error assertion to the existing negative test: it must fail because the current generic error lacks the ancestor. Apply the owner change, then run the existing inside/outside/symlink/marker controls and Ruff. Keep the original failure evidence. Full-suite baseline and isolated-candidate comparisons already running have no production edits.

## Implementation summary
Contract acceptance only; implementation follows in a separate commit.

## Cold diff audit
The same ancestors and marker predicate determine refusal. The error preserves its prefix and adds the selected marker. The existing test gains an exact message assertion; its original assertions remain.

## Gap audit
NOT DONE. Fail-first proof, implementation, publication/review and explanation of the original intermittent failure remain outstanding.

Accepted proposal patch SHA256: c9d66f147db90240f523ea86c40439e74263761b1f643239de4749ef245c611a
