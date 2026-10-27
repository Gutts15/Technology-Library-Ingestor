# E2 live-model synthetic smoke: deployment gate

The repository's E1/E2/E3 components are in main as **inactive** code. This
one-shot Actions workflow validates E2 and the private Drive bridge using
only an explicitly synthetic input and an isolated private `_teste` receipt.

- Trigger: first push of `.github/workflows/e2-synthetic-gemini-smoke.yml`
  to main; manual workflow_dispatch also supported.
- Permissions: contents: read only; concurrency serial; maximum nine minutes.
- Secrets: existing TL_RCLONE_CONFIG and GEMINI_API_KEY, never printed or saved.
- Reads the already-approved fixed 447-byte G0.3b synthetic input from
  `99_INBOX/CURATION/LOTES/_teste/g03-entrada.json` and checks exact SHA-256.
  Those bytes are **not** sent to the model.
- Builds a new **in-memory** synthetic E1-shaped technical-evidence item,
  calls the pinned Gemini free-tier model through the real E2 ask/validation
  code, and accepts only a conservative HOLD or REJECT outcome.
- Writes only a tiny metadata-only `e2-synthetic-recibo-YYYY-MM-DD.json`
  in the same private `_teste` folder with immutable write and readback.
  No user files, extracted content, prompts, source names or response body
  go to the GitHub logs or artifacts.
- On invalid model JSON, unexpected CREATE/UPDATE, quota, timeout, storage
  failure, or ambiguous write outcome, fail closed. Do not attempt canonical
  publication, retry an uncertain write, move data or auto-start the V1.

Safety: the existing zero-touch intake workflow remains `--fixture-only`.
This workflow is neither the E3 production deployment nor the V1 seven-day
acceptance. A PASS only proves one real Gemini E2 response and one private
Drive receipt on synthetically generated evidence.

Before routine release, E3 needs real coordinator + private Drive snapshots
and journal reconciliation, validated CREATE/UPDATE, strong cross-item
deduplication, the normal scheduled flow, and seven consecutive live days.
