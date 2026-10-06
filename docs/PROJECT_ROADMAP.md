# Technology Library — Public Roadmap

CHARTER_VERSION: 1.0
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
  kind and holds empty or weak-only content. See `STAGE21_EVIDENCE_GATE.md`.
  Real-file intake remains disabled.
- Stages 22–26: complete automatic relevance decisions,
  consolidation, safe canonical promotion, retrieval and bounded operation at
  R$0 mandatory additional recurring cost.
- Stage 27: pass the Charter's 500-item black-box acceptance test before the
  overall project can be declared complete.

No normal operation may require item-by-item review, an always-on user PC or
manual publication approval. The black-box acceptance test is NOT_RUN.
