# E3 private-Drive restart-recovery: one-shot isolated test

This workflow is **not the production publisher**. It is a single synthetic
acceptance test of the existing recovery fixture, with no user data or
canonical-record writes.

Execution: `.github/workflows/e3-isolated-drive-recovery.yml`.
First run is triggered by the workflow file's addition to main. Additional
runs require deliberate workflow_dispatch and are not scheduled. Permissions:
contents read. Uses existing TL_RCLONE_CONFIG, never prints secret values.

Trusted code path:
`src/candidate_production_restart_recovery_fixture.py --remote tl: --apply-remote-fixture`

The code chooses a random 24-hex-character synthetic fixture ID and maps
every logical `00_LIBRARY` path to the private isolated test folder
`99_INBOX/CANDIDATES/PRODUCTION_RESTART_RECOVERY_FIXTURE/<fixture-id>/00_LIBRARY/...`.
The actual production `00_LIBRARY` is never read or written.

The test uses a **temporary local bare Git repository** for exact-ref
coordinator ownership and an actual rclone connection to private Drive.
It writes and reads synthetic UPDATE/CREATE data, index bytes, snapshots
and journal, simulates interruption, starts a new Python process to recover,
verifies rollback, releases the coordinator only after recovered state, and
verifies a new transaction can acquire. The synthetic test folder is then
cleaned up and cleanup verified.

Failure handling: nonzero exit means the isolated test did not pass. The
fixture can report cleanup failure; operators must inspect only the synthetic
prefix. Never silently mark the V1 complete or activate the real DROP_HERE.

## What passing does NOT demonstrate

- Production GitHub coordinator ownership (test Git is local and disposable).
- Durable production transaction commit and real E3 CREATE/UPDATE.
- Recovery of delayed remote writes after timeouts with two live workers.
- Model-generated semantic correctness for user sources.
- Scheduled seven-day black-box acceptance.

This workflow is a gate towards E3 deployment, not production itself.
The `--fixture-only` intake switch remains untouched.
