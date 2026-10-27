# E3 Marco 2: integrated CREATE/UPDATE commit over private Drive fixture

This is an **isolated, real-Drive integration smoke**, not production ingestion.

It uses the E3 preflight, actual canonical transaction engine, verified receipt,
durable journal, Git CAS coordinator, immutable lot terminal sidecar and
idempotence checks in one synthetic batch with CREATE and UPDATE.

Every write made by this fixture is synchronously mirrored into a newly
generated private Drive prefix:
`99_INBOX/CANDIDATES/E3_COMMITTED_REMOTE_FIXTURE/<random-id>/`.
All logical `00_LIBRARY` paths are remapped below that prefix. The actual
`00_LIBRARY`, `DROP_HERE` and production `LOTES` are never mutated.

The Git CAS coordinator is a local, disposable bare Git repository. The fixture
proves integrated logic and the real Drive readback, not real GitHub-ref
coordinator release. Synthetic files are purged only after the commit,
terminal receipt, coordinator release and readback are all confirmed. On
uncertain writes the fixture fails closed, avoiding unsafe cleanup or retries.
Ordinary fixture failures may leave isolated artifacts requiring a separate
reconciliation; this is not authorized deletion of user sources.

The workflow is one-time on push to main or manually dispatchable, not a daily
publisher. The existing daily intake remains `--fixture-only`.

Passing this test confirms only the isolated E3 happy path on real transport;
the Marco 2 production-readiness gate still needs the actual GitHub-ref
fencing/lease, durable private journal transitions and recovery across a
lost-acknowledgment, demonstrated without leaking private data.
