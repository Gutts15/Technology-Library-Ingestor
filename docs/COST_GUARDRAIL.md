# Zero-Cost Architecture Guardrail

Status: ACTIVE
Version: 0.1.0
Last updated: 2026-09-10

## Requirement

The Technology Library must operate with **R$ 0.00 of mandatory additional financial cost** beyond the user's existing ChatGPT subscription.

This is an architecture requirement, not a preference.

## Forbidden as mandatory dependencies

Do not introduce any of the following as a required component of the normal system:

- paid OpenAI API usage;
- paid third-party LLM/API calls;
- paid SaaS automation services;
- paid vector databases or hosted retrieval services;
- infrastructure that can silently exceed a free allowance and create automatic billing;
- recurring paid monitoring or scheduled revalidation services.

A paid option may be documented only as optional reference. The working V1/V2 path must remain usable without it.

## Preferred execution order

Use, in order when technically adequate:

1. deterministic local code;
2. open-source tools;
3. local models through Ollama or equivalent zero-license-cost runtime;
4. services/resources already included in existing accounts, within non-billable limits;
5. human/ChatGPT-in-session semantic review when a zero-cost unattended path is not reliable enough.

If no safe zero-additional-cost path exists, stop/fail closed or leave the item pending rather than silently switching to paid infrastructure.

## Automation budget policy

Automation frequency must be demand-driven.

- Empty queues should terminate quickly.
- Do not revalidate canonical knowledge solely because time elapsed.
- Use stale/lazy revalidation when volatile information is requested again.
- Periodic monitoring applies only to explicitly watched items.
- Prefer batching candidate validation when it reduces compute/runtime without increasing risk.

## GitHub Actions

GitHub-hosted Actions may be used only while the workload stays within the user's already-included allowance and cannot create unwanted billing.

If scale threatens that condition, preferred migration is toward a self-hosted/local runner or reduced cadence, not a paid runner budget.

Do not enable a new high-frequency schedule until telemetry demonstrates it is justified.

## LLM policy

The repository must not require an `OPENAI_API_KEY` or other paid model API secret for normal operation.

Semantic stages should be designed so they can:

```text
resolve deterministically
OR
use a validated local model
OR
remain NEEDS_REVIEW
```

They must not turn uncertain knowledge into canonical knowledge merely to avoid human review.

## Storage policy

Primary and standby storage must remain within the user's existing no-additional-cost allocation. Processed raw media may stay excluded from standby when necessary to preserve free quota, while critical compact state is replicated.

## Change-review question

Before merging any new component, ask:

```text
Can this component cause the user to pay additional money in normal operation?
```

If yes, redesign it before enabling it as part of the default architecture.
