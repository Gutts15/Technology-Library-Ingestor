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
