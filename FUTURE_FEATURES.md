# Future Features: From QC Prototype to a Verifiable Quality Platform

> Product analysis date: 2026-11-11
>
> Status: concept design only. This document does **not** change the running codebase.

## 1. Product Readout

UA_LinqInspect is already more than a dashboard demo. Its strongest assets are:

- A standards-based OPC UA information model with 15 variables, 3 Methods, and 3 custom events.
- A stateful edge flow that subscribes to quality events, deduplicates, buffers, retries, and dead-letters MES deliveries.
- A clear separation between work-order ID, NG-order receipt, and completion receipt.
- A simulated AI source and a simulated MES, which make risky scenarios cheap to test.
- Validation evidence across the happy path, MES failures, timeout/retry behavior, and Method rejection.

The product opportunity is not to add more variables. It is to turn each quality decision into a **replayable, explainable, verifiable, and continuously improving business asset**.

The five features below are intentionally selected for user surprise and business value. They are not replacements for the security, state-consistency, and reliability work in `ROADMAP.md`; they are the differentiated capabilities that become possible once those foundations are stable.

### At a glance

| # | Feature | Primary user | Business outcome | Suggested horizon | Relative effort |
|---|---|---|---|---|---|
| 1 | Time-Travel Quality Twin | Quality engineer / incident responder | Reproduce and explain incidents in minutes instead of hours | Now / next | Medium |
| 2 | Governed Quality Copilot | Line supervisor / plant manager | Faster decisions with policy-constrained actions | Next | Medium–High |
| 3 | Continual Quality Learning Loop | ML engineer / quality engineer | Fewer escapes and faster model improvement | Next | High |
| 4 | Verifiable Quality Passport | Auditor / customer / recall investigator | Trustworthy proof for disputes, audits, and recalls | Next | Medium |
| 5 | Self-Healing Edge Guardian | OT/IT operations engineer | Less downtime, zero silent loss, safer recovery | Now | Medium |

## 2. Current Capability Map

| Current capability | What users can do today | Product gap the new feature closes |
|---|---|---|
| OPC UA event and Method model | Standard clients can inspect state, start/complete work orders, and subscribe to events | No way to replay or audit the full decision path after an incident |
| Node-RED queue and delivery state machine | Failures are retried, then moved to a dead letter | Recovery is still largely reactive and operator-driven |
| Split NG and completion receipts | Dispatch, receipt, and completion identities stay separate | Receipts are not portable, cryptographically verifiable evidence |
| Simulated inference | Safe experimentation without hardware | No model lineage, active learning, or shadow evaluation loop |
| Rich documentation and tests | Strong demo and handover story | No natural-language operational interface with governed actions |

## 3. Feature 1 — Time-Travel Quality Twin (Replay Studio)

**One-line pitch:** Replay any production window in an isolated digital twin, compare what happened with what should have happened, and export a forensic evidence bundle.

### User Story

> **As a quality engineer on call**, when I receive an alert about an NG-rate spike, a missing MES receipt, or a mismatched work order, I want to replay the exact event timeline and edge state transitions in an isolated twin, so that I can identify the root cause and prove it without stopping or writing to production.

### Technical Requirements

- **Canonical event envelope:** Every meaningful action must be recorded with `eventId`, `correlationId`, monotonic `sequence`, `eventType`, `occurredAt`, `ingestedAt`, `source`, `schemaVersion`, and a payload hash.
- **Capture coverage:** Record OPC UA Method calls and results, accepted and rejected variable writes, inspection events, queue transitions (`PENDING`, `SENDING`, retry, `SUCCESS`, `DEAD`), MES request/response bodies, receipt write-backs, configuration changes, and model-version metadata.
- **Replay engine:** Provide `observe`, `accelerated`, and `fault-injection` modes. The engine must use an injectable clock and deterministic random seed so simulation inputs can be reproduced exactly.
- **Isolated adapters:** Production MES and OPC UA adapters must be replaceable by sandbox adapters. A replay must have no outbound writes to the live MES or live OPC UA server.
- **State reconstruction:** Maintain periodic snapshots plus event-sourced replay so the system can reconstruct station state, queue state, deduplication state, and receipt state at any chosen sequence number.
- **Diff and investigation view:** Compare the original run with the replay and highlight first divergence, missing events, duplicate deliveries, late events, retry storms, and state mismatches.
- **Evidence bundle:** Export a signed, redacted incident bundle containing the event slice, configuration, replay result, and operator notes.
- **Interfaces:** Provide a CLI for CI and incident response, plus an optional read-only web view for engineers and auditors.
- **Performance target:** Replay an eight-hour shift in under five minutes in accelerated mode for deterministic components.
- **Acceptance signal:** A simulated lost-receipt incident can be reproduced from its event ID, assigned a root cause, and exported as evidence without touching production.

### Why It Surprises Users

Most QC prototypes can show live metrics. Very few can answer, “Show me exactly what the system believed at 14:32:07 and why it made that delivery decision.” Replay turns debugging from log archaeology into a repeatable product workflow.

## 4. Feature 2 — Governed Quality Copilot

**One-line pitch:** A natural-language operations copilot that explains quality events with evidence and can propose or execute only the actions the user is authorized to approve.

### User Story

> **As a line supervisor**, I want to ask “Why did NG rate rise on work order WO-20261109-001?” and receive an evidence-backed explanation with the relevant events, images, model version, queue state, and MES status, so that I can decide whether to continue, pause, replay, or escalate. When action is needed, I want to approve a specific recommended operation without giving the AI unrestricted write access.

### Technical Requirements

- **Tool gateway:** Expose a typed tool API, preferably through MCP or an equivalent internal interface, with separate read and action tools.
- **Read-first tool set:** Initial tools should include `get_quality_summary`, `get_work_order`, `get_event_window`, `get_delivery_state`, `get_model_health`, `search_runbook`, and `explain_first_divergence`.
- **Action tool set:** Mutating tools must call existing domain Methods or approved orchestration APIs, never raw OPC UA writes. Examples: `propose_start_inspection`, `propose_complete_work_order`, `propose_retry_delivery`, `propose_replay`.
- **Policy engine:** Enforce role-based access, station scoping, risk tiers, idempotency keys, preconditions, rate limits, and two-person approval for high-impact actions such as work-order completion or bulk replay.
- **Evidence grounding:** Every answer must cite event IDs, queue records, MES responses, document sections, or model metadata. The copilot must abstain or ask for clarification when evidence is insufficient.
- **Prompt-injection resistance:** Treat logs, MES payloads, and user-supplied text as untrusted data. Use allowlisted tools, schema validation, content isolation, and no credential or secret exposure in prompts.
- **Audit trail:** Record the user request, retrieved evidence, model response, proposed action, approver, execution result, and rollback path.
- **Human approval UX:** Present a before/after preview for every mutation, including affected work order, expected MES call, and dry-run result.
- **Provider abstraction:** Support a local/offline model option for factories that cannot send quality data to a public model provider.
- **Acceptance signal:** No action can be executed without policy approval and an audit record; every factual answer contains traceable evidence; an attempted prompt injection cannot trigger a write.

### Why It Surprises Users

The copilot is not a chatbot bolted onto a dashboard. It is an operations teammate with a narrow, auditable action surface. That combination of natural language and governed execution is the “wow” moment for supervisors and auditors.

## 5. Feature 3 — Continual Quality Learning Loop

**One-line pitch:** Turn production inspection evidence into a closed-loop model improvement system with active learning, shadow evaluation, drift detection, and safe promotion.

### User Story

> **As an ML engineer**, when the production model becomes uncertain or a new defect pattern appears, I want low-confidence and disagreement samples to flow into a review queue with full image, threshold, and model-version context, so that I can label them, train a candidate model, and prove it is better in shadow mode before promotion.

### Technical Requirements

- **Immutable model registry:** Track model name, version, artifact hash, framework/runtime, preprocessing version, confidence threshold, training dataset snapshot, offline metrics, and approval status.
- **Inference lineage:** Attach `modelVersion`, `preprocessorVersion`, `threshold`, `imageDigest`, `inferenceDurationMs`, and optional embedding reference to every inspection event.
- **Review queue:** Select samples based on low confidence, model disagreement, new defect clusters, random OK samples, and customer complaints. Support operator labels, label confidence, reviewer identity, and adjudication history.
- **Active-learning service:** Rank unlabeled samples by expected information gain or uncertainty, batch them for review, and produce reproducible candidate-training datasets.
- **Shadow inference:** Run candidate models asynchronously on the same image reference without changing production decisions. Compare candidate and production outputs per part and aggregate by defect, shift, station, and work order.
- **Drift detection:** Monitor confidence distribution, defect mix, input-quality metrics, and embedding drift. Alert when thresholds are exceeded, with links to affected work orders and suspected time windows.
- **Promotion workflow:** Require offline evaluation, shadow comparison, canary rollout, threshold approval, and rollback criteria before a model becomes the production model.
- **Data governance:** Define image retention, PII redaction, access control, export restrictions, and deletion policy. Never place raw images where the copilot or replay tool can expose them without authorization.
- **Acceptance signal:** Every production decision is attributable to a model version, and a candidate can be evaluated in shadow mode and rolled back within ten minutes without changing historical evidence.

### Why It Surprises Users

The prototype currently demonstrates inspection flow. This feature demonstrates a learning organization: every escape, uncertainty, and operator correction becomes a controlled improvement input rather than a forgotten log line.

## 6. Feature 4 — Verifiable Quality Passport

**One-line pitch:** Give every inspected part a tamper-evident digital passport that customers, auditors, and recall investigators can verify without trusting a single database administrator.

### User Story

> **As an auditor or customer quality engineer**, when a part is questioned during an audit, warranty claim, or recall, I want to scan its QR/NFC token and verify the inspection result, image digest, model version, work order, and MES disposition against a signed evidence chain, so that I can trust the record even if the operational database is later altered.

### Technical Requirements

- **Canonical evidence schema:** Define a versioned record containing `partSerial`, `workOrderId`, station ID, inspection timestamps, result, defect type, confidence, `imageDigest`, model version, threshold, operator/automation source, NG receipt, completion receipt, software version, and previous-record hash.
- **Hash chain:** Link each part record to the previous record using SHA-256 or an equivalent cryptographic digest. Support optional per-work-order Merkle roots for efficient batch verification.
- **Signing:** Sign station-level checkpoints with Ed25519 or equivalent. Keys must be held in a KMS/HSM or a documented development-only keystore; support rotation and revocation.
- **External anchoring:** Periodically anchor signed Merkle roots through an RFC 3161 timestamp authority or a customer-controlled immutable endpoint. Avoid claiming blockchain immutability unless an external anchor is actually deployed.
- **Verification service:** Provide a read-only API and offline CLI that can validate a passport, identify the first broken link, and show the signed root that contains it.
- **Physical token:** Generate a QR code or NFC payload containing a part ID and verification URL or offline proof. The token must not expose sensitive personal data.
- **Integration:** Create the passport at inspection completion and update it when an NG receipt or completion receipt arrives. Use an outbox/transaction boundary so a passport cannot claim a receipt that was not durably recorded.
- **Privacy and retention:** Store image hashes or controlled links rather than raw images in the passport. Enforce retention, redaction, jurisdiction, and customer-access policies.
- **Acceptance signal:** A single-byte modification to a stored record is detected; a valid passport can be verified offline from a signed bundle; no passport is issued until the required evidence is complete.

### Why It Surprises Users

Traceability is usually described as “we have the data.” A verifiable passport lets a customer prove that the data has not been silently rewritten. That changes the conversation from trust us to verify it.

## 7. Feature 5 — Self-Healing Edge Guardian

**One-line pitch:** Make the edge an autonomous recovery system that protects quality evidence, heals delivery after outages, and tells operators exactly what remains at risk.

### User Story

> **As an OT/IT operations engineer**, when the MES slows down, the OPC UA subscription drops, or the edge process restarts with a non-empty queue, I want the system to protect every accepted event, recover in a bounded and observable way, and show me a plain-language recovery plan, so that the line keeps producing without silent data loss or duplicate MES orders.

### Technical Requirements

- **Durable transactional outbox:** Persist queue items, deduplication state, retry counts, receipt state, and dead-letter records in SQLite with WAL or an equivalent transactional store. Commit enqueue and dedup state atomically.
- **At-least-once delivery contract:** Preserve per-work-order ordering, use idempotency keys, and make consumers idempotent. Do not advertise exactly-once delivery.
- **Subscription recovery state machine:** Model `CONNECTED`, `DEGRADED`, `RECONNECTING`, `RECOVERING`, and `FAILED`. Rebuild monitored items, detect event gaps, reconcile against a station snapshot, and expose the last known good sequence.
- **Adaptive delivery policy:** Add per-endpoint circuit breakers, jittered exponential backoff, `Retry-After` support, retry budgets, request timeouts, and dead-letter classification for retryable versus business-terminal failures.
- **Backpressure and loss prevention:** When the queue approaches capacity, stop accepting new work or spill to durable storage. Never silently discard the oldest pending event; raise an operator alert with an explicit recovery option.
- **SLO controller:** Track event-to-MES latency, queue age, delivery success rate, dead-letter rate, replay backlog, and loss budget. Produce a risk score with a reason, not just a red/green status.
- **Operator controls:** Provide pause, drain, replay, quarantine, and resume actions with previews, authorization, and audit logs. Include a one-click “recover safely” flow for common incidents.
- **Chaos and recovery tests:** Automate MES timeout/5xx, network partition, OPC UA restart, process crash, disk-full, clock-skew, and corrupted-queue scenarios. Verify that no accepted event is silently lost and no duplicate business order is created.
- **Acceptance signal:** The edge recovers from a simulated restart with a non-empty queue, drains the backlog in order, shows the exact recovery window, and produces zero silent losses with no duplicate MES orders.

### Why It Surprises Users

Most prototypes show a green status light until something breaks. This feature makes recovery itself a product experience: the system detects the fault, contains the blast radius, explains what it did, and gives the operator a safe next step.

## 8. Recommended Product Sequence

The features reinforce each other, so sequence matters more than raw effort:

1. **Self-Healing Edge Guardian** creates the durable event and delivery foundation.
2. **Time-Travel Quality Twin** reuses that event stream for replay and forensic analysis.
3. **Verifiable Quality Passport** reuses the same evidence model and adds cryptographic proof.
4. **Continual Quality Learning Loop** adds model lineage, review, and shadow evaluation to the quality event stream.
5. **Governed Quality Copilot** sits on top of the previous four and turns them into a natural-language operations experience.

This order delivers operational value early while keeping the ambitious AI and audit features grounded in trustworthy data.

## 9. Product Guardrails

- **No bypass writes:** Every new action must use the existing domain Methods or an explicitly governed orchestration API.
- **No false reliability claims:** Use at-least-once delivery with idempotent consumers and publish measured recovery behavior.
- **No opaque AI authority:** The copilot explains and proposes; humans approve high-impact actions unless a narrow policy explicitly allows automation.
- **No evidence without provenance:** Every quality result must remain attributable to an image, model version, threshold, station, and receipt.
- **No feature without an operational path:** Each feature must include observability, failure handling, retention, access control, and a rollback or recovery story.
- **No production data in demos:** Use synthetic or redacted replay bundles for portfolio demonstrations and external reviews.

## 10. Success Metrics for the Portfolio and Pilot

| Feature | Leading indicator | Business outcome |
|---|---|---|
| Time-Travel Quality Twin | Incident reproduction time | Lower MTTR and faster root-cause proof |
| Governed Quality Copilot | Time to evidence-backed decision | Faster supervisor response with fewer unsafe actions |
| Continual Quality Learning Loop | Shadow-model win rate and review throughput | Fewer escapes and faster model improvement |
| Verifiable Quality Passport | Passport verification coverage | Lower audit cost and stronger customer trust |
| Self-Healing Edge Guardian | Silent-loss count and recovery time | Higher line availability and lower reconciliation effort |

The most important product promise is not “more AI.” It is **trustworthy quality evidence that can be replayed, explained, verified, improved, and recovered under real factory failure modes.**
