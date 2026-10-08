# Email non-thinking policy and 9B qualification

## Contract

Operator accepts non-thinking as Email's target for both direct and gateway paths
("lets do it", 2026-10-07). The runtime/profile owns the mode. The actual pinned
Ollama 9B template closes an empty thinking block; no Invoice flag is copied.
Existing content-first reasoning fallback is preserved as a separate compatibility
policy. Necessity or removal is not decided by this qualification.

### Proven inventory and differences

Use docs/OLLAMA_EMAIL_ANALYSIS_QUALIFICATION.md as the recorded 30B comparator.
The existing benchmark and frozen analysis/obligation corpora own the oracle.
The candidate differs in model, template, model sampler defaults and loaded
context (32768 versus 8192). Request temperature0.1, analysis500 tokens,
scheduling1500, prompts, schemas, validators and labels were unchanged in the
first run. Production direct and gateway request projection matched on all22
frozen cases. Current wall-clock gateway expiry is a transport-only adapter
change; historical analysis timestamps stay inside the prompt.

Durable evidence: nonthinking-9b-qualification-20261007, outside worktrees.
One maximum144-call reservation stopped after67 submissions: direct54 analysis,
12 obligation, one scheduling failure. No gateway inference was reached, no retry
or replacement run is authorized by this record. Both deterministic corpus gates
passed; human semantic review and scheduling/gateway qualification remain open.
Never present an old unmatched private 30B packet as the remediated baseline.

### Root cause revision before implementation

The first scheduling response exposes two independent failure classes. It names
the correct attendee/time, but quotes prose containing the attendee address.
scheduling.py:1032-1038 treats that prose as an RFC email header using
getaddresses, losing mailbox identity. Introducing commit:
af8e00ecf7b3423593fcddd063f2676ddc4fd561 (strict scheduling extraction ledger).
This is a pre-existing validator defect, not a model-capacity hypothesis or
an implementation from PR217. The response also carries an ambiguity reason
with new_meeting; rejecting that contradiction is correct and remains required.

Correct invariant: an attendee's normalized complete mailbox must occur within
its quoted evidence AND within the source occurrence of that quote. Surrounding
prose is legal. Cropping the local part or domain out of a larger address must
not manufacture support. Source association, organizer/duplicate rules and
ambiguity admission remain mandatory. One scheduling evidence owner implements
this invariant, reusing the existing evidence-candidate and whitespace owners;
remove header parsing and its now-redundant address-validity helper.

### Required change surface

- scheduling.py: source-bound complete-mailbox evidence matcher at the point
  attendee support is decided.
- test_scheduling.py: public minimal prose reproduction, both boundary
  directions, cropped quotes, sibling mailbox forms and preserved ambiguity.
- Qualification record: distinguish the passing direct corpus gates from the
  stopped scheduling run and missing gateway/human qualification.

### Explicit non-scope

No prompt/model/config/runtime/default changes, fallback removal, corpus or label
edits, schema/cap changes, scheduling ambiguity relaxation, mailbox-header parser
changes, live retries, PR217 fixes, DocSum implementation or migration. This
revision permits only the isolated evidence-parser repair; it does not turn the
failed candidate into a qualified profile.

### Verification

Declare the prose-evidence regression fail-first. Then test complete-address
boundaries including cropped source quotes, good source/sender/display forms,
unknown source and evidence, organizer/duplicate behavior and ambiguity. Replay
the retained raw scheduling response without inference: attendee support must
be repaired, while new_meeting_ambiguous must still reject it. Run affected
scheduling/model/gateway/benchmark unit files and Ruff/format/diff checks.

### Acceptance

The operator accepted the attendee-evidence repair conditionally, relayed by
oversight at discussion_r4213328639: "I accept 218 once the tokenizer fix lands."
This replaces the earlier assertion that qualification authorization accepted
this new validator behavior. Oversight clears the acceptance hold after verifying
the one class-fix push; this session does not resolve that hold. Acceptance covers
the validator only. Scheduling/gateway/semantic gates still block 9B promotion.

### Tokenizer consolidation contract (before implementation)

Root cause: my commit18b641281ab294e7add19fbdd54667580bc9e2f1 introduced an
address-specific boundary expression in scheduling.py:_attendee_evidence_supported.
It treats a quote as a delimiter without consuming its quoted-local mailbox.
A direct production-validator probe accepts sender@example.com from both
"sender@example.com"@evil.com and the same syntax with whitespace before @.
The two-character neighboring-context workaround cannot establish whole identity.

Required change surface: one complete-mailbox tokenizer in scheduling.py,
yielding normalized literal tokens and their spans. Quoted local parts and their
escapes remain indivisible; angle brackets and prose punctuation delimit tokens.
Tokenize both quote and full declared source with that same owner. Acceptance
requires exact token equality at the quote's source occurrence, including its
full span; cropped quoted strings, longer local parts/domains and malformed
mailbox fragments cannot manufacture a smaller token. Remove the address-specific
boundary expression and neighboring-character window. Header parsing and address
normalization keep their existing owners; no scheduling schema or prompt change.

Assumptions/blockers: the source normalizer remains the address-validity owner.
A tokenizer may consume a malformed lexical mailbox without yielding support;
it must never search its interior for a smaller mailbox. Oversight acceptance
hold remains unresolved pending verification. No further inference is authorized
by this contract, and the stopped qualification counter remains cumulative.

Verification plan: fail-first public quoted-local and cropped-quoted regressions
through validate_scheduling_output; valid quoted-local/display/punctuation forms;
existing larger-local/domain negatives, repeated-source occurrences, wrong source,
empty/mixed/malformed tokens, escaped and spaced quoted locals. Then adjacent
scheduling/model/gateway/benchmark tests, Ruff and cold diff audit. Reuse retained
offline scheduling replay; attendee support is repaired while ambiguity rejection
remains. One class-fix push after this separate contract-only commit.

## Implementation and verification receipt

Fail-first prose regression:1 failed in0.37s with attendee_unsupported. After the origin repair:128 scheduling tests passed, then248 affected scheduling/model/gateway/benchmark tests passed in1.07s. An interim documentation artifact placement hit the frozen baseline inventory count; candidate metrics now remain in durable evidence rather than altering baseline fixtures. Final scheduling plus inventory regression:129 passed in0.34s; Ruff All checks passed; diff check clean. Cold AST audit changes only validate_scheduling_output, adds _attendee_evidence_supported and removes _is_valid_address; old test content is byte-preserved.

Offline retained-output replay repairs only attendee support and preserves new_meeting_ambiguous rejection. The isolated mailbox variant passes; larger local/domain cropped quotes admitted before are rejected after. No new generation, source prompt/schema/corpus/default change or model promotion. See docs/EMAIL_MODEL_POLICY_QUALIFICATION.md for the single Email policy/result record.

Gap audit: NOT DONE for full 9B qualification/promotion. Direct corpus safety is proven; actual gateway run, scheduling compatibility and independent semantic review remain outstanding. The bounded run stayed stopped at67 submissions.

### Tokenizer implementation receipt

Contract-only16e4c32731d0125272ab00c5bffb1a1be4ef6eaa preceded implementation.
The initial quoted-local reproduction failed12 cases before the fix because
attendee_unsupported was absent. The tokenizer now consumes whole lexical
mailboxes, including escaped quoted locals and folding whitespace/balanced
comments around @, and yields literal normalized identity with exact spans.
Both quote and full source use that owner. It removes my address-specific
boundary regex and two-character neighboring window from18b6412.

Final affected scheduling/model/gateway/benchmark check:282 passed in1.00s.
Ruff All checks passed; diff check clean. Original tests remain byte-preserved.
Cold AST audit: the only existing definition changed is
_attendee_evidence_supported; new helpers own tokens, spans and RFC comment
consumption. Empty, malformed, mixed/repeated, cropped quoted/local/domain,
escaped/unterminated and valid display/quoted/punctuation cases are covered.
Retained scheduling replay still rejects new_meeting_ambiguous; isolated attendee
support passes. No generation: cumulative67, gateway0, stopped reservation.
Durable tokenizer-audit.json and tokenizer-adjacent-tests.txt record this proof.

Gap audit: DONE for the accepted tokenizer origin repair. NOT DONE for fresh-head
CI/review, oversight acceptance-hold clearance and full 9B qualification.


## Grammar-by-source revision before implementation (historical)

The third-round revision below supersedes the ASCII prose grammar; source
selection, header parsing and occurrence matching from this revision remain.

Operator direction: address the threads. Oversight r4213450654 confirms the
existing validator acceptance covers this design correction without new approval.
My introducing 194ff60b7a32b947ca7d11d12475720a025029c5 mixed RFC header comments,
CFWS, bracketed domains and display names into prose. This is the inverse of the
original RFC parser applied to prose. Root correction note r4213849129 precedes
implementation; this revision is committed separately first.

Required surface: mailbox.py owns sender-header parsing with the real RFC parser,
normalization and exact raw mailbox spans. scheduling.py owns one strict prose
lexer for subject/body/attachment_name: dot-atom@dot-atom, parentheses and quotes
as punctuation; RFC-exotic compounds remain opaque. Both token streams feed the
existing exact identity and quote-occurrence span check. Remove the mixed RFC/prose
comment continuation and recursion owner rather than patch its four reported sites.

Tests: each bot finding belongs to its source grammar; valid sender comments and
CFWS pass, sender display names/comments/domain interiors cannot become attendees,
prose parenthetical mentions and quoted speech pass, exotic prose fails closed.
Keep cropped cases and add a context-bearing cropped quote while the same address
appears whole elsewhere. That negative must fail when occurrence equality is
mutated to address-anywhere. Fail-first grammar cases, adjacent scheduling/mailbox/
model/gateway/benchmark tests and Ruff; reuse retained offline qualification replay.

Non-scope: existing recipient_addresses behavior, model/runtime/settings/prompts,
normal profile and schema unchanged. No new generation/retry/gateway run or model
promotion. Ambiguity, live gateway and semantic review remain qualification gates.
Hold stays for independent exact-head verification; one correction push.


## Third-round class consolidation before implementation

### Root cause
My 194ff60 prose lexer and 9b1f60d source-grammar correction independently
restated mailbox validity in an ASCII _DOT_ATOM regex (scheduling.py:442-443),
and split opacity by quoted/bracketed constructs (scheduling.py:436-441).
The existing config.py:247 normalize_validated_address admits Unicode local
parts and IDN domains, but that duplicate grammar rejects them. Treating angles
as unconditional separators extracts an inner address from a larger compound.

### Required change surface
scheduling.py owns only maximal lexical boundaries and complete literal spans.
One balanced enclosure scanner keeps all compounds intact, including whitespace,
escapes, quoted speech, brackets and angles. Surrounding display brackets,
parenthetical mentions and standalone prose quotations can supply boundaries;
embedded enclosure syntax stays opaque. Whitespace/comments around an @ belong
to the same opaque compound. Candidates have exactly one @ and normalization
must equal the literal run's casefold; config.normalize_validated_address alone
owns mailbox validity. Remove the ASCII grammar and separate quote/bracket regex
owners. Preserve source-selected header parsing and exact quote-occurrence spans.

### Explicit non-scope
No header-parser, configuration normalizer, prompt/model/runtime/default/schema,
corpus, retry, live gateway, semantic qualification or promotion changes.
No downstream attendee filter or address-specific patch.

### Assumptions and blockers
Oversight review 5450823568 accepts this class design with no new acceptance
needed; root note precedes edits and contract-only commit precedes code.
One consolidation push. After this fix, false acceptance blocks; additional
unusual valid syntax rejected is grouped in one follow-up issue and deferred,
as instructed in operator comment 6051238588. Current Unicode/angle cases are
part of this consolidation, not deferred. Promotion gates remain unchanged.

### Verification plan
Declare fail-first real-validator cases for Unicode local/IDN rejection and
both angle compound orientations falsely supporting an inner attendee. Cover
body, subject and attachment sources, full/cropped quotes, correct display
angles, quoted speech/locals, parentheses, escaped/unclosed enclosures, mixed
valid/invalid runs, empty/single/large inputs and exact spans. Retain all earlier
regressions; scheduling/adjacent tests and Ruff/diff checks. Remove normalizer
equality, compound opacity, source selection and same-occurrence membership
independently: the respective regression tests must fail. Replay the retained
original completion offline, preserving scheduling ambiguity rejection and
without generating another model response. Cold audit each changed file.


### Third-round implementation receipt

The declared Unicode/angle real-validator reproduction failed 18 cases before
implementation. The boundary-only scanner now consumes entire enclosure compounds;
configuration normalization admits literal candidates. Removed the duplicate ASCII
regex and separate quoted/bracketed regex owners. Existing header/source/occurrence
functions and prior tests are unchanged. All affected suites: 347 passed; Ruff and
diff checks pass. Six mutations fail tracked tests: occurrence, both source-grammar
swaps, opacity, literal identity and reinstated ASCII rejection. An initial opacity
harness mutation failed to apply; it was corrected and rerun, and is not counted
as regression proof. Offline original completion still rejects solely for
new_meeting_ambiguous, with no model generation or completion rewrite. Durable
literal-owner-receipt.json and literal-cold-diff-audit.md bind source and artifacts.

Gap audit: local class repair DONE; published-head independent verification and
CI pending. No promotion; scheduling ambiguity/live gateway/semantic review stay.


## Linear scanner revision before implementation

Root cause: my 061f1b7 _prose_mailbox_tokens recursively queues whole-run
parenthetical/quoted regions and _prose_enclosure_end rewalks their interiors.
The repeated text slices are also quadratic; indexing matches alone must not
leave those slices in the wrapper loop. Origin scheduling.py:439-453, :505-522.
Oversight 4219687137 reproduced scaling and directs one owner correction.

Required surface: compute enclosure match ends once with a stack pass; all
boundary walkers share that index. Nested region traversal uses index bounds,
constant-time match lookups and no full-interior copies. Materialize a literal
candidate only after surrounding speech/parenthetical wrappers are removed.
Preserve opacity, normalizer ownership, source grammar and occurrence matching.

Input bound: production service.py:629-633 constructs SchedulingSource with
bounded_gateway_text(content.body, MAX_GATEWAY_BODY_CHARS); model.py:60 sets
100000. SchedulingSource and direct tokenization impose no independent cap.
No new cap or source truncation in this change.

Non-scope: no syntax acceptance change, normalizer/header parser, prompt/model,
settings, gateway run, retry, promotion or deployment work. Existing false
rejection from unmatched quotes is deferred to the grouped follow-up issue.

Verification: fail-first operation-count regression at depth20000 for both
parentheses and quoted parentheses; count indexing and copied slice lengths so
quadratic rescans/copies fail quickly. Original published probe depths and
mixed/enclosure semantic tests remain. Keep all six existing mutation probes
and retained original completion replay unchanged. Affected suites and Ruff.
One correction push after root note and contract-only commit.

### Linear scanner receipt
Two depth20000 operation-budget cases fail before and pass after. One match
index is built once; nested wrappers traverse ranges without copying interiors.
All earlier tests byte-preserved, source and occurrence definitions AST unchanged.
Affected suites349passed; six original mutations still fail, offline completion
still rejects ambiguity with no generation. The stray-quote false rejection is
tracked in issue220. Native/model defaults and source input bounds unchanged.
New-head CI/review remain pending after the one correction push.
