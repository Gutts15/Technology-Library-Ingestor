# Stage 21 — Synthetic cloud semantic qualification

Stage 21 is IN_PROGRESS. Real intake remains fixture-only. This experiment
qualifies a possible no-user-PC execution path; it does not replace the
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
- Serial concurrency, 20-minute qualification-job ceiling, two serial benchmark
  batches of five cases, each with a 480-second ceiling including preload, and
  bounded response size. This is not a change to daily intake/session limits.
- Only synthetic evidence and synthetic public-source excerpts. No raw model
  response, source text or rationale is printed or retained; diagnostics use
  validated disposition enums, candidate counts and fixed reason codes.

Runtime and model metadata are verified against their official distributions:
[Ollama release](https://github.com/ollama/ollama/releases/tag/v0.40.0),
[Ollama Qwen3.5 9B](https://ollama.com/library/qwen3.5:9b) and
[Qwen model card/license](https://huggingface.co/Qwen/Qwen3.5-9B).

## Observed results and corrections

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

The next step is a broader independent evidence/claim benchmark, followed by
private automatic pipeline integration and end-to-end synthetic acceptance.
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

The R$0 execution basis is a public repository using standard GitHub-hosted
runners, no retained artifacts/cache, no paid model provider and no user PC.
[GitHub billing documentation](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
states that standard public-repository runners are free and larger runners are
charged. No larger runner or paid fallback is authorized by this experiment.
