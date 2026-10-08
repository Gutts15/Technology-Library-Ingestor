# Stage 21 — R$0 production executor assessment

Reviewed: 2026-10-08. Status: NOT_VERIFIED. This assessment records alternatives;
it does not approve a provider/account prerequisite, provision infrastructure, change the production
model, enable real intake or alter the Charter.

## Decision

Keep standard public GitHub Actions runners for bounded synthetic development
tests. Do not use them as the routine private Library processing backend.
The user reaffirmed the original Drive/GitHub baseline on 2026-10-08 after an
Oracle suggestion was incorrectly presented as the next required setup step.
Review existing resources first, as recorded in `EXECUTION_DECISIONS.md`.
Oracle Always Free Arm remains an untested compatibility alternative for the
existing Python/FFmpeg/Ollama worker; it is not an adopted provider or an
approved user obligation. Cloudflare Workers AI is a possible hosted inference
component, not a drop-in executor for this pipeline. Neither candidate is
qualified or connected. No paid plan, trial-dependent resource or automatic
upgrade is part of this decision. The initial-setup exception in the Charter
must not be used to infer approval of an additional provider dependency.

## Verified provider facts and remaining gaps

| Option | Facts checked in official documentation | Project assessment |
| --- | --- | --- |
| GitHub Actions, standard public runners | Runner usage is free for public repositories. Additional terms limit VM access to development/testing and restrict serverless/application uses. | Suitable for the current synthetic software qualification. Not selected for daily private knowledge processing. Free minutes alone do not prove permitted production use. |
| Oracle Always Free A1 | Current documented allowance: 1,500 OCPU-hours and 9,000 GB-hours/month, equivalent to 2 OCPUs and 12 GB RAM; 200 GB combined boot/block volume in the home region. Small applications are an intended use. Capacity shortages and idle-instance reclamation are documented. Signup requires identity/card verification; no SLA. | Unqualified alternative; no account prerequisite is approved. Actual capacity, Arm runtime, performance and recovery would need proof if a provider change were later adopted. Do not assume the historical 4-OCPU/24-GB allowance. |
| Cloudflare Workers Free + Workers AI | AI includes 10,000 neurons/day; free-limit exhaustion returns errors instead of paid overage without upgrade. Some models require paid billing and are excluded. Workers Free: 10 ms CPU, 128 MB RAM, 5 cron triggers; scheduled wall time is 15 minutes. Workers AI says customer content is not shared with other customers or used for training/improvement without consent. | Possible free inference/orchestration component. These Worker CPU/memory limits cannot host the current local model and FFmpeg pipeline. Would require a different model/transport, quality qualification and a separate free media executor; not selected as a full replacement. |
| Hugging Face CPU Basic Spaces | CPU Basic has no hourly cost and provides 2 vCPU/16 GB RAM, but current documentation requires a paid plan to create a Gradio or Docker Space. Free static Spaces cannot execute this Python worker. | Excluded for a new deployment under the no-additional-subscription requirement. Do not equate zero hourly hardware pricing with free account eligibility or use ZeroGPU availability as evidence for unattended CPU execution. |

The project assessments above are engineering inferences from the documented
limits and current worker design, not provider guarantees or account-level
verification. No provider quota, billing mode, capacity, credentials or deployment
has been verified in an actual production account.

## Qualification before integration

This checklist is conditional reference material for a later approved alternative.
It is not the current execution order and must not trigger a signup request.
The current first step is the existing-resource assessment in
`EXECUTION_DECISIONS.md`.

1. Verify a single eligible Always Free VM in the account's home region. Record
   shape, RAM, quota totals and free-resource classification privately. Reject
   any configuration relying on expiring trial credits, paid upgrade or resources
   outside the free region/allowance. Card verification, if needed, is an initial
   account-administration step, never routine item review.
2. Verify downloaded Arm bytes against their own official archive checksum.
   The v0.40.0 release API checked on 2026-10-08 reports
   `ollama-linux-arm64.tar.zst` (1,557,877,510 bytes), SHA-256
   `4d27cb1d8f46176a3c0ce10aea2a16e4ad73dfe1599f2ed13468f29e5b8aba0d`.
   This is verified expected metadata, not a downloaded-byte or Arm execution
   PASS. The same API's x86 digest matches the existing qualification pin.
   Verify the model manifest before
   any prompt; keep inference loopback-only, cloud disabled and no fallback.
3. Measure model peak RAM and latency with the same frozen original/holdout
   suites. Preserve the 90-second inference deadline, bounded responses and
   worker/session limits. A successful four-thread x86 test does not prove that
   a two-OCPU Arm VM meets these limits. Hold failures automatically.
4. Exercise synthetic decoding for every supported media type, including OCR,
   audio/video transcription and spreadsheets. Measure total worker RAM/time,
   not just text inference. No private Drive content belongs in public CI.
5. Install secrets outside code/history and disable content-bearing logs. Verify
   a private-storage canary, fail-closed quota behavior, idempotent receipts,
   expired-lease recovery and next-day resumption after interruption.
6. Prove recovery from unavailable/reclaimed compute without loss of source or
   processing state. Retain authoritative state in private Drive; no artificial
   load to evade an idle policy, PC dependency or manual per-item work queue.
7. Only after these checks, integrate the bounded daily worker and run the
   representative scheduled daily-flow acceptance, including empty and single-item
   cycles and interruption recovery. A provider comparison, model smoke or free
   account alone cannot satisfy the Charter's end-to-end test. A 500-item load
   test is optional and must not become a user-supplied dataset requirement.

## Official sources

Checked on the review date; reverify before provisioning or changing a provider.

- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub additional product terms, Actions section](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions)
  (effective 2026-08-27)
- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [Oracle Free Tier FAQ](https://www.oracle.com/cloud/free/faq/)
- [Ollama v0.40.0 official release metadata](https://api.github.com/repos/ollama/ollama/releases/tags/v0.40.0)
- [Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [Workers AI data usage](https://developers.cloudflare.com/workers-ai/platform/data-usage/)
- [Hugging Face Spaces overview, compute creation requirements](https://huggingface.co/docs/hub/spaces-overview)
