# Stage 20 — Zero-touch Drive intake (implementation, not acceptance)

The source is only `99_INBOX/DROP_HERE` on the private `tl:` remote. One
default-branch GitHub Actions schedule starts a session daily at 06:17 UTC.
There is no polling during the rest of the day. A worker with remaining
eligible backlog dispatches exactly one successor using the native Actions
API and `GITHUB_TOKEN`. The Drive worker has read-only repository permission;
only the separate, secret-free dispatch job has `actions: write`. Neither job
uploads artifacts or runs in pull requests. The existing concurrency group
serializes workers with the private-storage ingest workflow.

Before enabling the schedule, run its one-time `baseline_existing` dispatch on
reviewed `main`. This creates private revision receipts for items already in
DROP_HERE **without copying, routing, or processing them**. It refuses a second
baseline. The scheduled job remains inert until the repository variable
`TL_STAGE20_ENABLED` is `true`; the worker also refuses to run without the
private baseline marker. This prevents a newly installed schedule from
processing pre-Stage-20 real backlog. These are one-time deployment controls,
not routine user steps.

While Stage 20 cadence acceptance is underway, the deployed workflow also
passes `--fixture-only`: it selects only the explicitly named synthetic test
family and excludes **all** other items, including any new real upload. The
general discovery implementation remains available, but removing this safety
gate requires a separate reviewed change after acceptance.

At daily start, a private session record freezes the eligible source/revision
pairs observed in the inbox. Newly uploaded or revised files wait for the
next daily cycle, so continuous uploads cannot prolong a session indefinitely.
The private record holds the UTC cycle, worker count, start time and remaining
opaque digests. Public dispatch input carries only a Boolean continuation
flag; no source name, provider ID, Drive URL or private session identifier.
The final worker closes the session and schedules nothing else that day.

Discovery lists only the inbox. Each source is keyed by the SHA-256 digest of
its provider ID; a missing provider ID is held rather than treating its name
as a stable identity. A revision digest uses size, modification time, and
available provider hashes. State is one JSON receipt per source, stored only
under the private Drive process log. Source names and IDs never enter public
state or normal logs. Files are copied to deterministic pseudonymous queue
names; the source remains in DROP_HERE so subsequent revisions are observable.

Each worker enqueues at most five objects and 512 MiB, with a 512 MiB per-object
limit, a 15-minute processing budget, a 20-minute lease, and a 20-minute runner
timeout. Backlog is drained through sequential workers, up to an exceptional
ceiling of 24 workers or eight hours per session. Remaining work after that
ceiling, a stalled lease, or a failed worker is reconsidered automatically on
the next daily cycle. The lease records intent before copy; after an
interruption, the next eligible worker checks the same destination and records
completion without making a duplicate copy. A source changed during copy is
held for a later revision. An empty daily session performs one check and no
successor dispatch.

A separate scheduled keepalive checks daily and makes a public **empty commit**
only if the latest repository commit is at least 30 days old. It has no Drive
secret, no private inputs, and no artifact; its write permission is the sole
narrow exception in the security gate. This mitigates GitHub's automatic
disabling of schedules after 60 days of public-repository inactivity. It is
not a guarantee against an extended GitHub scheduler outage or repository
policy blocking bot pushes, so cadence acceptance must verify both jobs.

The intake worker only places selected items into existing private media
queues. It does not change the Ingestor Core or authorize processing the
pre-Stage-20 backlog. Stage 20G acceptance must demonstrate with synthetic
fixtures: 0 items => one daily no-op; 1–5 => one worker; 8, 12 and 30 items
=> two, three and six serial workers respectively; no duplicate copies;
failure recovery; no dispatch after drain; no extra daily polls; and clean
public logs/artifacts. Stage 20 is not DONE until those checks pass. Oversized
or unsupported provider objects remain deferred/held and need a future bounded
policy before universal intake can be claimed.
