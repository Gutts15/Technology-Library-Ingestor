# Technology Library — Public Roadmap

CHARTER_VERSION: 1.1
PROJECT_STATUS: IN_PROGRESS

The public repository holds reusable automation code only. The private Drive
remains the authority for source media, processing state and canonical records.

- Stage 19: DONE — the clean-history automation repository is public. The
  publication gate and bounded post-publication private-storage canary passed;
  private Drive content and credentials remain outside the public repository.
- Stage 20: DONE — the daily zero-touch intake session passed fixture-only
  acceptance: scheduled no-op, serial two-worker backlog drain, and the next
  scheduled post-drain no-op. Real-file intake remains disabled pending a
  separately reviewed change; checked public logs/artifacts showed no private
  exposure.
- Stage 21: IN_PROGRESS — synthetic FFmpeg scene-sampling validation passed;
  the automatic candidate boundary now assesses extracted evidence by media
  kind and holds empty or weak-only content. Candidate subjects must match
  verified source content and usable file text independently. See
  `STAGE21_EVIDENCE_GATE.md`. Draft bounded textual acceptance now passes all
  ten original and twenty independent frozen cases; the actual opt-in bridge
  passes five synthetic live-model/storage cases, including repeat idempotency
  and conflict holding. Shared empty-evidence startup preparation fits existing
  budgets and uses no benchmark facts. A permitted R$0 production executor,
  actual multimodal/private integration and full pipeline acceptance remain open.
  See `STAGE21_CLOUD_PROOF.md`.
  Provider terms/limits are assessed in `STAGE21_PRODUCTION_EXECUTOR.md`:
  GitHub Actions remains a development-test executor. Resume with the existing
  Drive/GitHub baseline assessment in `EXECUTION_DECISIONS.md`; no Oracle
  account or replacement cloud provider is an approved prerequisite.
  The earlier Arm VM suggestion is an unqualified alternative, not a deployment
  decision or a new user obligation.
  Real-file intake remains disabled; these draft changes are not deployed.
  Raw-file synthetic integration now passes twelve local and CI cases with real
  PDF/XML/spreadsheet decoding, image/video OCR and the actual candidate/receipt
  boundary; source probes/model replies remain stubs. See
  `STAGE21_MULTIMODAL_PROOF.md` for scope and remote verification status.
- Stages 22–26: complete automatic relevance decisions,
  consolidation, safe canonical promotion, retrieval and bounded operation at
  R$0 mandatory additional recurring cost.
- Stage 27: pass representative scheduled daily-flow end-to-end acceptance,
  including empty/single-item days, mixed small batches and automatic recovery.
  A 500-input load experiment is optional; the user need not accumulate a bulk
  dataset. The full automatic publication/index/retrieval path remains required.

No normal operation may require item-by-item review, an always-on user PC or
manual publication approval. The black-box acceptance test is NOT_RUN.

Next: verify the uncovered production capability against existing resources,
preserve the current code and complete independent multimodal integration
checks. Do not provision a replacement provider or revise the Charter by
inference from this roadmap.
