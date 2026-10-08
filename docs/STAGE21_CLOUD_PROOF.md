# Stage 21 — Synthetic cloud semantic qualification

Stage 21 is IN_PROGRESS. Real intake remains fixture-only. This experiment
tests model viability without the user PC; it does not establish eligibility
to host routine production processing, replace the
production model or authorize canonical promotion.

The branch-scoped `cloud-semantic-proof.yml` runs CPU inference on a standard
ephemeral GitHub Actions runner. It has no Drive access or repository secrets,
uses read-only repository permission, publishes no artifacts, and deletes its
temporary runtime and model directory. It has no daily schedule or main trigger.

## Reproducible controls

- Ollama v0.40.0 Linux archive SHA-256:
  `c94aa4156b3d13e64ebc2efe5ea53f015384c882be776e6695cfb37fb180d5ad`.
- Current experimental Qwen3.5 9B GGUF manifest SHA-256:
  `56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90`.
- Loopback server only; Ollama cloud disabled; no paid API or remote inference.
- Workflow runs are serialized; six fixed qualification jobs (two original and four holdout
  windows), each with a 20-minute ceiling and exactly one batch of five cases.
  At most two independent synthetic jobs run concurrently, on separate ephemeral
  VMs; inference within each job stays serial. Each batch has a 480-second ceiling including preload, and
  bounded response size. This is not a change to daily intake/session limits.
- Only synthetic evidence and synthetic public-source excerpts. No raw model
  response, source text or rationale is printed or retained; diagnostics use
  validated disposition enums, candidate counts and fixed reason codes.

Runtime and model metadata are verified against their official distributions:
[Ollama release](https://github.com/ollama/ollama/releases/tag/v0.40.0),
[Ollama Qwen3.5 9B](https://ollama.com/library/qwen3.5:9b) and
[Qwen model card/license](https://huggingface.co/Qwen/Qwen3.5-9B).

## Observed results and corrections

The next bounded qualification reruns both frozen suites after a generic prompt
clarification: missing technical evidence (bookmarks, bare names or marketing)
is distinct from evidence of an accidental personal upload. Expected outcomes,
input digests, model identity and transport limits remain unchanged. Each window
now runs in a separate ephemeral job, so earlier windows cannot consume
the last window's job budget. Every window must pass; no aggregate success is
claimed while any case is failed, skipped, cancelled or unverified.

Run 37765753645 resolved the confirmed URL-only disposition mismatch, but its
first original window timed out on useful English input and an independent CSV
case returned invalid JSON. These failures are not approvals. A follow-up
clarifies concise rationale/summary fields while preserving all directly
supported core claims, without increasing time or token budgets. The harness
now rejects mismatched model identities, incomplete responses and explicit
token-limit truncation before parsing; only fixed reason codes are logged. The
experimental production transport also holds explicitly truncated responses,
even if a prefix happens to parse as JSON. The full frozen suites rerun after
the prompt change; the CSV failure's precise cause is not yet established.

The concise-output follow-up resolved the CSV JSON failure, but useful input
again timed out on the first chat in each useful-content window. This is an
observed startup pattern, not proof of its cause. A shared, optional startup
self-test now sends empty synthetic evidence through the same pinned transport,
schema and production prompt structure. It must return no candidate and an
insufficient-evidence disposition before readiness is established. It never uses
benchmark facts or private input, writes no records, and has a 90-second ceiling.
The semantic benchmark counts this preparation inside its existing 480-second
total; inference limits and frozen expectations stay unchanged. Both proof
workflows use the same preparation. This helper is not wired into the daily
production worker or the legacy default model.

The shared concurrency group uses `queue: max` with cancellation disabled so
one proof cannot discard another pending proof. This preserves serialized
workflow runs and at most two independent benchmark VMs per run. There is no
scheduled inference or automatic retry loop.

| Run | Scope | Result |
| --- | --- | --- |
| 37540080150 | Runtime bootstrap | Download redirect body rejected by checksum before inference; redirect handling corrected. |
| 37540187686 | Qwen3 1.7B, original six cases | 4/6 passed in 67 seconds; irrelevant and contradictory evidence produced unsafe candidates. Not qualified. |
| 37540682652 | Qwen3 4B, unchanged six cases | 4/6 passed in 141 seconds; irrelevant evidence produced a candidate and the first inference failed. Not qualified. |
| 37541180719 | Qwen3 4B, eligibility-first prompt and preload | 5/6 passed in 145 seconds; noise/conflict/injection were held, but useful Portuguese evidence was missed. Not qualified. |
| 37541692617 | Qwen3 4B, bilingual clarification and four unseen holdouts | 9/10 passed in 247 seconds. Useful Portuguese evidence was incorrectly SUSPECTED_ACCIDENTAL with no candidate. Not qualified. |
| 37563527781 | Qwen3.5 4B, frozen ten cases with existing combined prompt | 8/10 passed in 335 seconds. Portuguese content was recognized, but two other useful cases were incorrectly rejected. Not qualified. |
| 37564246051 | Qwen3.5 4B, two-step prototype | 6/10 passed in 114 seconds; all useful cases failed before extraction. Not qualified. |
| 37564653759 | Same prototype with fixed-code diagnostics | 6/10 passed in 48 seconds; all four useful selections failed `selection_unbound_evidence`. Instructions omitted a named-quote constraint required by validation; aligned without relaxing the validator. |
| 37564889059 | Two-step prototype, named-quote instruction aligned | 6/10 passed in 120 seconds; two useful selections remained unbound and two other useful cases were HELD. Not qualified. |
| 37565477280 | Qwen3.5 9B, combined production prompt, unchanged ten cases | 10/10 passed: two serial five-case batches in 147 and 136 seconds, including preload. Bounded viability PASS; not full production qualification. |
| 37566524224 | Same pinned model and prompt, twenty frozen independent holdouts | 17 PASS, one confirmed disposition mismatch, two UNVERIFIED extraction failures during job cancellation at the 20-minute ceiling. Expanded acceptance INCOMPLETE; not qualified. |

The production prompt's unconditional instruction to propose exactly one
candidate was replaced by disposition-first, conditional extraction. The same
rules apply across languages and unknown tool names. A valid technical subject
must still be directly supported; familiarity or an accessible source alone is
not sufficient. None of the original six acceptance expectations was weakened.

The suite additionally tests unfamiliar technical subjects, Portuguese noise,
incidental household sequences and Portuguese contradictions. A passing result
is only a bounded viability proof. It cannot establish production accuracy,
ground every possible claim, validate multimodal extraction, consolidation,
canonical transactions or the Charter's 500-item acceptance test. Those require
separate evidence before real input can be enabled.

HELD/NEEDS_REVIEW remains machine-managed, never routine user review.

## Checkpoint and next gate

The experiment remains a draft, not a production model change. The earlier
1.7B/4B configurations are NOT_QUALIFIED. Qwen3.5 9B passed the frozen bilingual
suite without changing any expected outcome: all four useful cases produced
the expected subject, and all six negative/conflict cases produced no candidate.
This resolves the bounded no-user-PC viability gate, not the full quality gate.

The independent evidence/claim benchmark below did not qualify. Its confirmed
disposition mismatch and two interrupted cases must be resolved without relaxing
the frozen acceptance. A permitted R$0 production executor must also be verified
before private automatic integration and end-to-end synthetic acceptance.
It must preserve the free normal path, private execution inputs and logs,
conservative handling of noise/conflicts, and useful-content recall. A passing
model smoke alone does not complete Stage 21 or the full automatic pipeline.

## Two-step experimental path

The newer model did not resolve combined relevance/extraction reliably. The
experiment separated subject selection from candidate extraction.
Selection returns only a disposition, a verbatim subject and a contiguous source
quote. Deterministic validation requires the technical subject to occur in the
source and quoted passage; rejected/conflicting/insufficient inputs carry no
candidate text. Only a bound technical selection reaches extraction.

This is a different experimental prompt path, not a test of the unchanged
production caller. It retains all ten frozen inputs and expected outcomes, uses
the existing candidate schema and plan validator, and does not change production
execution. A quote proves provenance, not truth or complete claim support. Broader
grounding and the automatic pipeline remain separate acceptance requirements.
That 4B two-step configuration was not qualified. It is preserved as an optional
experimental mode, not adopted as production behavior. The successful 9B test
returned to the combined production prompt on the same standard free CPU
runner, keeping the ten expectations frozen and splitting them into two serial
five-case benchmark batches. Raising this qualification
job's ceiling to twenty minutes accommodates both batches; it does not authorize
paid infrastructure, real input or changes to daily intake limits.

## Public execution privacy coverage

The full available logs of all eight jobs in runs 37564246051, 37564653759,
37564889059, 37565477280, 37565481023, 37565481041, 37565477091 and 37565477185
were checked deterministically. No email addresses, private Drive URL patterns,
high-confidence token/private-key patterns or personal Windows user paths were
found in this checked coverage. Each run had zero artifacts. The model proof
uses only the frozen synthetic inputs, prints fixed result codes and has no
Drive credentials. These checks are bounded evidence, not proof of absence
outside the inspected logs or proof of future private-data execution safety.

The five additional runs at commit
`1a9f425489ed58866bf6b9f48ce88926d784bba8` (37566524224, 37566527186,
37566527259, 37566524366 and 37566524255) had five jobs total. All available
complete job logs passed the same deterministic pattern checks; all five runs
had zero artifacts. These were synthetic software checks, not private intake.

## Independent expanded acceptance (INCOMPLETE)

The 2026-10-08 continuation strengthens the candidate bridge with independent
subject checks for public-source content and usable extracted file text. It
preserves the experimental pinned CPU transport, default model, frozen suites
and historical qualification verdicts. Synthetic boundary checks now reject
unrelated source/file subjects, URL-only file samples, metadata-only matches and
blocked-speech matches without creating a candidate. Existing receipts remain
idempotent. Fresh local verification passed 32 unit/integration tests plus four
standalone bridge/planning smoke scripts, security, alignment and cost gates.
These tests use synthetic model responses; no new live-model accuracy run or
production activation is claimed. The expanded acceptance below remains
INCOMPLETE, and the production-executor gate remains NOT_VERIFIED.

After the ten-case viability PASS, twenty new synthetic textual cases were
defined before running the unchanged model and combined prompt. Their
canonical JSON SHA-256 (sorted keys, compact separators, UTF-8 without ASCII
escaping) is `8b3a3dcde79143669acd159783c0bbc5bbada727d4d97543f392d001bd6fce69`.
The unit suite locks both inputs and expected outcomes to this digest.

Ten useful cases cover stream parsing, protocols, API diffs, OCR-like and
transcript-like excerpts, mixed noise, resolved version differences, encoding,
bounded retries and format conversion. Ten negative cases cover incidental
technical words, unsupported marketing, URL/name-only inputs, unresolved
contradictions, unreadable OCR-like output, role/JSON injection and domestic
sequences. These are synthetic text excerpts, not a live multimodal decoding
or private-storage integration test.

Every useful case must preserve its subject and core capability markers in
the extracted claims. Specified unsupported integrations, certification,
subscription and performance claims are rejected in both summary and claims.
Every negative case must create zero candidates. All twenty must pass; do
not relax expectations after observing output. Lexical probes only detect the
specified errors; they do not prove complete semantic grounding.

Run 37566524224 executed four serial five-case batches, preserving the
model/runtime pins, inference settings, 20-minute job ceiling, zero artifacts,
no Drive access and no production/schedule changes. The original ten-case
suite is retained unchanged and selectable; its prior PASS is not substituted
for this independent acceptance.

All ten useful holdouts passed. Of the eight completed negative cases, seven
passed and `url_only` failed: it returned `SUSPECTED_ACCIDENTAL` instead of the
frozen `NO_REUSABLE_KNOWLEDGE`/`NEEDS_REVIEW` expectation, with zero candidates.
This is a confirmed classification mismatch, not an observed private write or
unsafe candidate. Do not change the expectation to accommodate the observed
result. `json_injection` and `household_pipeline_pt` returned `extraction_failed`
immediately before job cancellation; both remain UNVERIFIED, not established
model accuracy failures. The workflow conclusion was `cancelled`, not success.
The earlier ten-case PASS remains bounded evidence and cannot override this
expanded INCOMPLETE result. No additional live inference has been scheduled.

## Opt-in private candidate integration (not activated)

`pinned_cpu_inference.py` provides an explicit CPU-only transport. It validates
the model tag and manifest digest before sending a prompt, disallows proxies
and redirects, retains the existing strict loopback/port policy, caps prompt
size at 8 KiB and response size at 64 KiB, and shares a maximum 90-second
request budget across inventory and inference. Invalid responses fail closed
without printing content or calling another provider. The prompt byte ceiling
is a resource guard, not proof of complete token/context coverage; long evidence
needs separate bounded-chunk acceptance before real intake can be enabled.

The existing candidate bridge has an explicit `--pinned-cpu-model` opt-in;
its legacy default is unchanged. Real loopback HTTP protocol tests and fixture
storage tests cover pin rejection, redirects, malformed/oversized input/output,
weak-evidence rejection, one candidate plus receipt, repeat idempotency and no
canonical writes. These checks do not establish live-model integration accuracy.

`cloud_candidate_bridge_smoke.py` prepares a five-case live-model acceptance
through the actual opt-in bridge and ephemeral local fixture storage. It stubs
only the verified public-source probe with synthetic text; it does not fetch
real source pages or use Drive. Four useful cases must create exactly one private
candidate each, repeat without another inference/write, and preserve the source
subject; one contradiction must be held without a candidate. Live execution
is pending. No routine schedule or production mode has been enabled.

## Production hosting eligibility gate

The free public-runner billing rule does not establish permission to run a
general production backend. The current
[GitHub additional terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
limit hosted-runner activities to the associated software lifecycle and also
restrict disproportionate/serverless application use. The synthetic proofs
here test this repository's software. Routine private Library processing is
not assumed eligible: production hosting on Actions remains NOT_VERIFIED and
must not be enabled on the strength of these CI results. No inference was added
to the daily intake workflow.

Alternative hosting has not been provisioned. Current
[Hugging Face Spaces documentation](https://huggingface.co/docs/hub/spaces-overview)
requires a paid plan to create ordinary compute-backed Gradio/Docker Spaces;
zero hourly CPU price alone is not R$0 account eligibility. The separate
[ZeroGPU documentation](https://huggingface.co/docs/hub/spaces-zerogpu)
describes a free-account exception with verified email, account-age requirements,
Gradio-only hosting and a five-GPU-minute daily quota. That exception has not
been verified for account access, this workload, privacy or production capacity;
it is not a qualified drop-in executor. Current
[OCI Always Free documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
lists 2 OCPUs/12 GB for Always Free Arm tenancies, capacity constraints and idle
resource reclamation. OCI is only a candidate pending account access, actual
capacity, runtime/privacy proof and explicit setup authority. No account, trial,
paid upgrade, new repository or production migration was created. Preserve
the current code and private Drive authority while resolving this gate.

The R$0 software-testing basis is a public repository using standard GitHub-hosted
runners, no retained artifacts/cache, no paid model provider and no user PC.
[GitHub billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
states that standard public-repository runners are free and larger runners are
charged. No larger runner or paid fallback is authorized by this experiment.
