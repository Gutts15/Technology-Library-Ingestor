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
- Qwen3 4B manifest SHA-256:
  `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`.
- Loopback server only; Ollama cloud disabled; no paid API or remote inference.
- Serial concurrency, 15-minute job ceiling, 480-second inference ceiling
  including preload, and bounded response size.
- Only synthetic evidence and synthetic public-source excerpts. No raw model
  response, source text or rationale is printed or retained; diagnostics use
  validated disposition enums, candidate counts and fixed reason codes.

Runtime and model metadata are verified against their official distributions:
[Ollama release](https://github.com/ollama/ollama/releases/tag/v0.40.0),
[Ollama Qwen3 4B](https://ollama.com/library/qwen3:4b) and
[Qwen model card/license](https://huggingface.co/Qwen/Qwen3-4B).

## Observed failures and corrections

| Run | Scope | Result |
| --- | --- | --- |
| 37540080150 | Runtime bootstrap | Download redirect body rejected by checksum before inference; redirect handling corrected. |
| 37540187686 | Qwen3 1.7B, original six cases | 4/6 passed in 67 seconds; irrelevant and contradictory evidence produced unsafe candidates. Not qualified. |
| 37540682652 | Qwen3 4B, unchanged six cases | 4/6 passed in 141 seconds; irrelevant evidence produced a candidate and the first inference failed. Not qualified. |
| 37541180719 | Qwen3 4B, eligibility-first prompt and preload | 5/6 passed in 145 seconds; noise/conflict/injection were held, but useful Portuguese evidence was missed. Not qualified. |
| 37541692617 | Qwen3 4B, bilingual clarification and four unseen holdouts | 9/10 passed in 247 seconds. Useful Portuguese evidence was incorrectly SUSPECTED_ACCIDENTAL with no candidate. Not qualified. |

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

The experiment remains a draft, not a production model change. Qwen3 1.7B and
this Qwen3 4B configuration are NOT_QUALIFIED. The repeated useful-Portuguese
failure must not be hidden by changing its fixture, weakening the expected
outcome, accepting partial success or enabling real input. No more prompt-only
iterations on this same configuration are justified by this experiment.

The next step is to qualify a different no-user-PC inference configuration on
the frozen bilingual suite, then a broader independent evidence/claim benchmark.
It must preserve the free normal path, private execution inputs and logs,
conservative handling of noise/conflicts, and useful-content recall. A passing
model smoke alone does not complete Stage 21 or the full automatic pipeline.
