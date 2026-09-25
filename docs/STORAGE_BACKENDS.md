# Storage backends

Last updated: 2026-09-10
Status: Google Drive primary / OneDrive standby / candidate-aware replication code active

The ingestor must not depend on one cloud vendor. Storage is an interchangeable private inbox/queue layer behind rclone.

## Current operating model

```text
PRIMARY
Google Drive / remote `tl:`
        |
        +--> normal ingest
        |
        +--> critical-state replication
                    |
                    v
STANDBY
OneDrive / remote `tl_backup:`
```

Google Drive is the preferred primary while healthy. OneDrive is the operational standby. Media processors consume `TL_ACTIVE_REMOTE` and remain provider-neutral.

## Zero-additional-cost constraint

Storage must remain within the user's existing no-additional-cost allocations. Do not introduce a paid storage tier as a required dependency. Processed raw media remains excluded from standby when necessary to conserve quota.

## Required backend behavior

A backend is suitable when it can support:
- private phone/desktop upload;
- rclone list/download/upload/move operations;
- the Technology Library root layout;
- files of at least roughly 250 MB each;
- no raw-media publication to GitHub;
- isolated credentials;
- predictable operation without additional financial cost for the expected personal workload.

## Primary compatibility

The workflow supports Google configuration through existing repository secrets and also supports a generic rclone primary `[tl]` remote.

Secrets and OAuth material must never be written to logs, candidate records or canonical knowledge.

## Standby configuration

The current standby uses the `tl_backup:` alias rooted under the dedicated OneDrive folder `Technology-Library-Backup`.

When the backup secret is absent, the workflow can still run primary-only. In the current deployment the standby is configured.

## Critical-state replication

`src/storage_replica.py` maintains the standby copy.

### Pre-ingest scopes

Candidate-aware pre-ingest replication now covers:

```text
99_INBOX/DROP_HERE
99_INBOX/TO_REVIEW
99_INBOX/CANDIDATES
```

This minimizes the recovery window for both newly uploaded raw inputs and research candidates captured directly from ChatGPT conversations.

### Post-ingest scopes

The full post-run continuity snapshot now covers eight scopes:

```text
00_LIBRARY
99_INBOX/DROP_HERE
99_INBOX/TO_REVIEW
99_INBOX/CANDIDATES
99_INBOX/READY_FOR_ANALYSIS
99_INBOX/PROCESS_LOG
99_INBOX/CURATION
99_INBOX/ERROR
```

Deliberately excluded:

```text
99_INBOX/PROCESSED
retention trash
replica trash as source data
```

Standby synchronization uses `rclone sync` with destination-side `REPLICA_TRASH` backup directories so replaced/deleted destination files are quarantined instead of immediately destroyed.

A successful replication writes:

```text
99_INBOX/PROCESS_LOG/STORAGE/replication-state.json
```

Current marker contract after candidate-layer migration:

```text
complete = true
phase = post
scope_policy = critical_state_v1
scope_count = 8
age <= 24 hours
```

The policy label remains `critical_state_v1` for compatibility; `replica_version` was bumped to 0.2.0 because the protected scope set expanded.

## Migration note: 7 -> 8 scopes

The last validated production standby marker predates the candidate queue and therefore reports seven post scopes.

`src/storage_failover.py` now requires eight scopes. Until the next legitimate production ingest finishes with the updated code, automatic failover will conservatively reject the old seven-scope marker if primary storage is unavailable.

Do not manufacture an empty ingest merely to improve cadence telemetry. Refresh the marker on the next legitimate normal or maintenance run. A healthy primary remains usable before that refresh.

## Guarded failover

Normal selection:

```text
primary healthy
-> TL_ACTIVE_REMOTE=tl:
-> TL_STORAGE_MODE=primary
```

Primary outage with valid standby:

```text
primary unavailable
+ backup reachable
+ fresh complete 8-scope POST marker
-> TL_ACTIVE_REMOTE=tl_backup:
-> TL_STORAGE_MODE=fallback
```

When failover activates, the standby receives:

```text
99_INBOX/PROCESS_LOG/STORAGE/failover-active.json
```

While that lock exists, the standby remains authoritative even if Google becomes reachable again. This prevents split-brain failback.

## Controlled reconciliation / failback

`src/storage_reconcile.py` imports the current `POST_SCOPES` set from `storage_replica.py`, so candidate state participates in future failback reconciliation automatically after the new marker is established.

The path remains:

```text
OneDrive active during outage
-> Google returns
-> sync current critical scopes OneDrive -> Google
-> quarantine displaced primary state
-> validate
-> refresh replication marker
-> remove locks only after success
-> validate again
-> Google may become primary
```

Quarantine root:

```text
99_INBOX/FAILBACK_QUARANTINE
```

The operation remains fail-closed.

## Candidate queue relationship

Chat research candidates live under:

```text
99_INBOX/CANDIDATES/CHAT_RESEARCH
```

They are compact pending knowledge, not canonical records. Because they may be created outside the raw-file ingest workflow, the whole `99_INBOX/CANDIDATES` tree is now critical compact state and is included in pre/post replication.

See `docs/CANDIDATE_QUEUE.md`.

## Validation history

Before candidate-layer extension, the storage layer had already passed synthetic and real tests for replication, stale-marker rejection, failover locking, failback reconciliation, quarantine behavior and provider recovery.

The candidate-aware extension has passed repository synthetic/security checks. Real primary-to-standby replication with the new eight-scope marker remains pending the next legitimate production ingest.

## Candidate ranking for future storage changes

- **OneDrive**: current cloud standby.
- **Local/self-hosted storage/runner**: preferred long-term path if cloud quotas or GitHub-hosted runtime ever threaten the zero-additional-cost requirement.
- **Other free cloud providers**: fallback candidates only if they satisfy file-size, quota, privacy and rclone requirements.

## Migration rule

Media processors must never care which provider is active:

```text
storage reconciliation / selector
      |
      v
TL_ACTIVE_REMOTE
      |
      v
queue reconciliation / router / planner
      |
      v
media processors / candidate maintenance
      |
      v
READY / CANDIDATES / PROCESS_LOG / CURATION
```

Changing primary or standby must remain a storage-boundary operation, not a media-pipeline rewrite.

## Current next actions

1. Refresh the standby to the new eight-scope marker on the next legitimate production ingest.
2. Validate that `99_INBOX/CANDIDATES` is present and equal on standby after that run.
3. Keep cron disabled until cadence evidence is sufficient.
4. Continue building the candidate validator/deduplicator without introducing paid APIs.
