# AGENTS.md

This repository is governed by the Technology Library Project Charter.

Before making architectural changes, starting a new top-level stage, changing operator flow, or declaring a milestone complete, read in this order:

1. `docs/PROJECT_CHARTER.md`
2. `docs/PROJECT_CONTRACT.json`
3. `docs/PROJECT_ROADMAP.md`

This public snapshot deliberately excludes private operational state, including
the historical `docs/CURRENT_STATE.md` and live curation decisions. Do not add
those files to this repository; obtain private runtime state from private storage.

## Authority

`PROJECT_CHARTER.md` is authoritative for product intent.

If technical documentation, roadmap history, memory, prior agent output or implementation convenience conflicts with the Charter, stop and flag the conflict. Do not silently reinterpret the goal.

## Non-negotiable agent rules

- Do not add recurring human review or approval to the normal path unless the Charter is explicitly revised by the user.
- Do not infer a scope change from approval of a technical step.
- Do not replace automation with manual operation for safety convenience.
- Do not add mandatory recurring cost or an always-on user PC dependency without explicit Charter change.
- Do not declare the overall project DONE while the Charter black-box success test is not PASS.
- A subsystem/core release may be complete while the overall North Star remains incomplete.
- Low-confidence information should fail closed into machine-managed states such as HELD; HELD must not silently become a user work queue.
- Prefer reversible, auditable automation: evidence gates, deterministic policy, snapshots, recovery and quarantine.
- Charter 1.1 uses representative daily-flow acceptance; 500 inputs is an optional stress example, never a required user dataset or minimum processing threshold.

## Execution premise correction — 2026-10-08

The original execution baseline was private Google Drive plus the existing
GitHub automation repository at zero mandatory additional recurring cost.
No Oracle account, replacement cloud provider or migration is an approved
prerequisite. Read `docs/EXECUTION_DECISIONS.md` before resuming executor work.

Evaluate existing resources first. Separate a verified provider limitation
from an engineering inference and an untested alternative. Do not present a
provider candidate as a user obligation. The Charter's initial-setup exception
does not authorize imposing an unapproved provider/account dependency.

Preserve the existing code, private storage authority and frozen acceptance
criteria. If the baseline cannot meet them, record the exact uncovered
capability and a tested alternative before proposing a material dependency
change. Continue independent work; do not declare the project complete or
activate real intake from subsystem test results.

## Alignment Gate

At every top-level stage boundary and before a release, explicitly verify:

```text
ALIGNMENT: PASS|FAIL
CHARTER_VERSION: <version>
NEW_ROUTINE_USER_STEPS: <count>
NEW_MANDATORY_RECURRING_COST: <amount>
USER_PC_DEPENDENCY: yes|no
BLACK_BOX_STATUS: <status>
```

Any FAIL blocks the stage/release until resolved or the Charter is explicitly revised.
