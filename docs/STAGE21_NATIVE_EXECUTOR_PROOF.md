# Native scheduled execution — bounded proof

Reviewed: 2026-10-08. Status: PARTIAL. Real intake remains disabled.

## Existing-resource evidence

An isolated one-time task used the existing ChatGPT subscription and existing
Drive/GitHub connections. It ran without an active foreground Work session,
personal PC, new account, paid API or per-run approval. Its only external writes
were private synthetic probe files in the existing SECURITY_SMOKE directory.
The one-time task completed and is disabled; no daily production task was enabled.

The first run executed Python and wrote its private result, but direct public
repository cloning failed. This is an observed transport failure, not proof that
GitHub code is inaccessible. A retry materialized twelve source files through
the existing GitHub app, verified their Git blob SHAs at checkpoint
`276ddb1e9c5c3903de02b5312d7b0b8ecdb753a0`, and passed all 24 selected intake,
daily-session and alignment tests. The runtime had FFmpeg and Tesseract, but
neither rclone nor a loopback model. The private result was read back and verified.

## Native semantic experiment

A second one-time run generated actual native ChatGPT responses for the same
ten original and twenty frozen textual holdouts. The input packet contained
evidence and the production prompt/schema, with anonymous case IDs; it did not
contain expected outcomes, expected titles or the evaluator. Shared prompt
prefix/suffix compression was checked to reconstruct all thirty prompts exactly.
The task did not fetch the synthetic URLs or read canonical/private source data.

The parent independently evaluated the actual stored responses with the unchanged
`build_plan` and `check_result` gates: **30/30 PASS**. The backend is explicitly
`native_scheduled_chatgpt_unpinned`; this is neither a pinned-model release nor an
assurance about all future native model changes. Frozen cases provide bounded
behavior evidence, not general semantic correctness or end-to-end acceptance.

| Evidence packet | SHA-256 |
| --- | --- |
| Input, 20,383 bytes | `9ac995ff8a67a541c2145b0b1650478f9c516505a6da57d9e8f4375e52070125` |
| Actual native responses, 11,106 bytes | `bea1fcf1219bc35fde5abd046c37187b7f11f167c5893b308c246c8b9b319076` |

Packets and operational task/file identities remain private. No media, private
evidence, credentials or content-bearing logs were published.

## Rework assessment and thin integration

Decision: TEST FIRST. Preserve existing Drive authority, repository, frozen
quality criteria, candidate/receipt boundaries, transaction/index/recovery code
and the legacy/pinned CPU defaults. No replacement storage, provider migration
or new recurring user action is introduced. The uncovered transport assumptions
are rclone and loopback inference in a native scheduled runtime.

The first integration slice adds `native_model_exchange.py` and two explicit
bridge options: `--prepare-native-request` and `--native-response`. Preparation
requires current bound file evidence and verified source access; it creates no
candidate. Consumption revalidates both and hashes the prompt, schema, package,
revision, envelope, evidence digest and fetched source digest. Changed context
holds the response. Strict packet parsing rejects duplicate keys, non-JSON
constants, extra fields and oversized input. Native errors have no model/API
fallback. All existing model, independent file/source subject, candidate and
receipt gates still apply. Neither option writes canonical records.

Seven native transport regression cases passed. The actual thirty captured
native responses were then applied through the real bridge with ephemeral local
storage: **30/30 PASS**, **14 candidates created**, **16 held**, and **14 repeat
checks without duplication**. Source fetches in this integration remain synthetic
stubs. Eligible prompts and schemas matched those actually presented to the task.
This does not establish real public source access, private storage integration,
multimodal native analysis or automatic canonical publication.

Reproduce with private synthetic packets (never real intake):

```text
python3 scripts/native_semantic_bridge_probe.py --inputs <input-packet> --responses <response-packet>
python3 tests/test_native_model_exchange.py
python3 tests/test_candidate_daily_cycle.py
```

## Remaining completion gates

The native runtime and private write capability are now demonstrated; a blanket
claim that no existing unattended executor has been demonstrated is outdated.
The complete product remains unqualified. Integrate a bounded Drive app storage
port, verify actual public-source acquisition and supported media including
speech, connect native semantic/claim/consolidation decisions to an automatic
publication policy, preserve exclusive coordination and durable private recovery,
then run scheduled daily-flow acceptance through canonical indexes and retrieval.
Do not manufacture operator attestations to make the current controlled publisher
appear unattended. Do not activate real intake or declare DONE from these proofs.

ALIGNMENT: PASS for this bounded experiment and optional transport integration.
CHARTER_VERSION: 1.1. NEW_ROUTINE_USER_STEPS: 0.
NEW_MANDATORY_RECURRING_COST: R$0. USER_PC_DEPENDENCY: no.
BLACK_BOX_STATUS: NOT_RUN.
