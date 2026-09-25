# Technology Library Ingestor

Public automation code for the Technology Library. Private source media,
processing state, credentials, extracted evidence and canonical knowledge stay
in private storage; they are not part of this repository.

The existing core contains bounded ingestion, multi-format processing,
candidate validation, transaction safeguards, index rebuilding and recovery.
The full zero-touch product is still in progress. In particular, ordinary
Drive intake and safe canonical promotion must become unattended before the
project meets its [Charter](docs/PROJECT_CHARTER.md).

The repository is designed for R$0 mandatory additional recurring cost and
normal operation without the user's PC. Workflows that need private storage
read credentials from GitHub Actions secrets at runtime. Never commit a live
rclone config, private curation decision file, media, OCR or transcript.

The bundled retrieval cases are synthetic examples, not a copy of live Library
state. Supply private validation cases only through private storage when a
real-library check is required.

Run the local privacy and alignment checks with:

```text
python scripts/security_gate.py
python src/project_alignment_guard.py
python tests/test_retrieval_regression_cases.py
```

The [public roadmap](docs/PROJECT_ROADMAP.md) tracks the remaining stages.

## Public project status

Stage 19 (public automation boundary) is DONE. The clean-history repository is
public, and the bounded post-publication private-storage canary passed in
[Actions run 36191129517](https://github.com/Gutts15/Technology-Library-Ingestor/actions/runs/36191129517)
with zero artifacts and no private values found in its public logs. The private Drive
and its contents remain outside this repository.

Next is Stage 20 — Zero-touch Drive intake. The overall project remains
IN_PROGRESS; the Charter's 500-item black-box acceptance test is NOT_RUN.
