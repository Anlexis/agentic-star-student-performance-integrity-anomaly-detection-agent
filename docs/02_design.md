# Template Design Specification - EDU-C2-010

## Position in the AgentCore Architecture

- **Agent Class**: StudentPerformanceAnomalyDetectionAgent
- **Category**: Cat 2 (multi-step domain workflow, two-layer nested architecture)
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic - msgpack incompatible);
    structured fields are stored as JSON strings (`to_json`/`from_json`)
  - Node: framework inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution); the domain
    workflow is encapsulated in an inner `BaseGraph` wrapped by a `GraphNode`

| Layer | Class |
|---|---|
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |

## Composition Pattern (Cat 2 two-layer nested)

- **Outer graph** (`src/graph/graph.py`): `StudentPerformanceAnomalyDetectionAgent(AgentBaseGraph)`
  - fixed 5-node backbone. The `main` slot is `DomainWorkflowGraphNode(GraphNode)`,
  which delegates to the inner graph.
- **Inner graph** (`src/graph/domain_workflow_graph.py`): `DomainWorkflowGraph(BaseGraph)`
  - the full domain pipeline (5 domain nodes, linear topology). Implements all
  7 `BaseGraph` ABC methods. `get_output()` is designed together with the outer
  `DomainWorkflowGraphNode.merge_output()`.
- **Context bridge** (`src/graph/context_bridge.py`): `GraphNode.execute()` does
  not forward `input_context` on `subgraph.invoke()`, so the outer
  `extract_input()` stashes it in a `ContextVar` and the inner
  `_extra_initial_state()` seeds it back into the inner state.
- **Error propagation strategy**: `propagate` (inner errors re-raised as
  `SubgraphError`, fail-fast).

```
Outer backbone (fixed):
  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                                             (RETRY, max 3 -> pre_process)

Inner graph (DomainWorkflowGraph):
  START -> validate_input -> load_cohort_baseline -> detect_deviations
        -> classify_anomaly -> generate_intervention_alert -> END
```

## Architecture Overview

### Node Configuration

#### Outer backbone

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | Set schema_version, session_id, trust_level | user_input | (framework defaults) | InitializeNode (default) |
| pre_process | Input gate: parse + validate the JSON anomaly request envelope; surface-strip PII | user_input, input_context | validated_input, anomaly_request | PreProcessNode (FunctionNode) |
| main | Delegate to the inner DomainWorkflowGraph; bridge input_context | validated_input, input_context | result, intervention_alert, anomaly_type, status | DomainWorkflowGraphNode (GraphNode) |
| post_process | Output gate: credential/PII scan, verbatim redaction, precision grids | result | formatted_output, result, status | PostProcessNode (FunctionNode) |
| finalize | Build response_metadata, total_time_ms | result | response_metadata | FinalizeNode (default) |

#### Inner graph (DomainWorkflowGraph)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| validate_input | Caller-data contract, fail-closed: identifiers, period format, finite+bounded numerics | anomaly_request, input_context, thresholds_config | anomaly_request (normalised) | ValidateInputNode (FunctionNode) |
| load_cohort_baseline | Load cohort baseline metrics from the baseline service | anomaly_request | cohort_baseline | LoadCohortBaselineNode (FunctionNode) |
| detect_deviations | Compute per-metric z-score / percentile / anomaly flag vs. baseline | anomaly_request, cohort_baseline | deviation_scores | DetectDeviationsNode (FunctionNode) |
| classify_anomaly | Classify anomaly_type + confidence from flagged metrics | deviation_scores | anomaly_type, anomaly_confidence | ClassifyAnomalyNode (FunctionNode) |
| generate_intervention_alert | Map to recommended action + severity; render the alert (aggregates only) | anomaly_type, anomaly_confidence, deviation_scores, thresholds_config | intervention_alert, result, status | GenerateInterventionAlertNode (FunctionNode) |

### State Definition

| Field | Type | Storage | Purpose | Required |
|-------|------|---------|---------|----------|
| validated_input | str | native | Surface-PII-stripped request echo (inner-graph entry string) | Yes |
| anomaly_request | str (JSON) | to_json/from_json | {student_id, data_type, period, thresholds, metrics} | Yes |
| thresholds_config | str (JSON) | to_json/from_json | Config-seeded {z_score_threshold, confidence_high, confidence_medium} | Yes |
| cohort_baseline | str (JSON) | to_json/from_json | {metric: {mean, std, p25, p75, n_students}} | Yes |
| deviation_scores | str (JSON) | to_json/from_json | {metric: {z_score, percentile, is_anomalous, source}} | Yes |
| anomaly_type | str | native | grade_drop / attendance_gap / submission_timing / similarity_flag / multi_signal / no_anomaly | Yes |
| anomaly_confidence | float | native | Overall classification confidence [0.0-1.0] | Yes |
| intervention_alert | str (JSON) | to_json/from_json | {anomaly_type, confidence, severity, recommended_action, evidence_summary, data_type, schema_note} | Yes |
| result | str (JSON) | native | Serialised final alert for response_metadata | Yes |
| trace_id / correlation_id | str | native | Tracing/audit - framework-managed (do NOT write from node code) | - |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- Structured fields stored as JSON strings (`to_json`/`from_json`) - never bare dict/list in a checkpointed field (msgpack safety)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)

## Configuration

`config/agent.yaml` is the static manifest (identity, entry point, trust level).
`config/config.yaml` carries the runtime parameters, loaded by the registry and
forwarded to the inner graph by `DomainWorkflowGraphNode._parent_config()`:

| Key | Default | Meaning |
|-----|---------|---------|
| `max_retry` | `3` | Outer backbone retry budget |
| `timeout_s` | `30` | Per-invocation timeout |
| `anomaly_thresholds.z_score_threshold` | `2.0` | abs(z) at/above which a metric is flagged |
| `anomaly_thresholds.confidence_high` | `0.7` | confidence at/above -> severity HIGH |
| `anomaly_thresholds.confidence_medium` | `0.4` | confidence at/above -> severity MEDIUM |

The `anomaly_thresholds` block is republished into inner state as the
JSON-string field `thresholds_config`; the domain nodes read live values from
it, with the module constants as documented fallbacks.

## Caller-Data Contract

`POST /invoke` accepts the request envelope as `input` and structured
invocation data as `input_context` (adapter cap: 256 KiB). Every
caller-controlled field is validated **fail-closed** in `ValidateInputNode` -
a violation terminates the run with a field-NAMING error, and the offending
value is never echoed into errors or logs.

| Field | Channel | Rule |
|---|---|---|
| `student_id` | request | `[A-Za-z0-9_-]{1,64}` opaque identifier |
| `data_type` | request | one of grades / attendance / submission / similarity |
| `period.start` / `period.end` | request | ISO `YYYY-MM` or `YYYY-MM-DD`, or empty |
| `thresholds.z_score` | request | finite number in [0.5, 10.0] |
| `metrics.<name>` | `input_context` (primary) or request (fallback) | name must be the data_type's known metric (inert identifier); value finite and inside explicit per-metric bounds; at most 8 entries |

Non-finite numerics (`NaN`, `±Infinity`) are rejected explicitly on every
numeric field: they parse via `float()` and arrive intact through raw JSON,
and NaN comparisons are always False - an unchecked NaN threshold would
silently suppress every alert, failing OPEN on the exact decision this
template exists to make.

Per-metric bounds: `grade_delta` [-100, 100], `attendance_rate` [0, 1],
`submission_timing_offset` [-720, 720], `similarity_score` [0, 1].

## Output Contract

The alert renders cohort-relative **aggregates only**, each rounded to 2
decimal places, and carries a `schema_note` stating that contract. Raw measured
values are dropped before rendering. `PostProcessNode` then enforces the
contract independently, in four layers:

1. **Credential / student-PII scan** - API keys, JWTs, Bearer tokens, raw
   credential assignments, or a student e-mail address anywhere in the alert
   withhold the output entirely (sanitised stub, status ERROR).
2. **Verbatim caller-text redaction** - a verbatim embedding of caller-derived
   state text is replaced with `[REDACTED]`, with an audit event.
3. **Decimal precision grid** - any decimal token carrying 3+ fraction digits
   is snapped to 2 decimals, with an audit event.
4. **Raw-magnitude grid** - comma-grouped numbers, unformatted 5+-digit runs,
   and currency-marked values (marker before or after the value, attached or
   separated by any whitespace run, signed or unsigned) are snapped onto a
   units-of-1,000 grid, with an audit event. Structural tokens (bare counts,
   years, 2-decimal statistics) stay byte-identical.

Composition guarantees layers 3 and 4 today; the gate enforces them anyway, so
a future rendering change cannot silently break the contract.

## Framework Utilization

### Shared Components Used
- [x] Input validation - PreProcessNode rejects empty / oversized / non-JSON / missing-field / unsupported-data_type input before the inner workflow runs
- [x] Output gate - a module-level scan in GenerateInterventionAlertNode (credentials + student e-mail in the alert) plus the authoritative module-level gate run inside PostProcessNode.execute()
- [x] Audit logging - `emit_trace_event()`, at least one domain event per node `execute()`:
  - PreProcessNode -> `pre_process_validated`
  - ValidateInputNode -> `validate_input_complete`
  - LoadCohortBaselineNode -> `cohort_baseline_loaded`
  - DetectDeviationsNode -> `deviations_computed`
  - ClassifyAnomalyNode -> `anomaly_classified`
  - GenerateInterventionAlertNode -> `intervention_alert_generated`
  - PostProcessNode -> `post_process_formatted` (plus a redaction event per gate layer that fires)

> **Security-gate behaviour by node type:**
> - `FunctionNode` subclass -> the framework's `@final` gate always runs
>   automatically; extend via `_extra_security_gate_input()` /
>   `_extra_security_gate_output()` only. This template never overrides
>   `_security_gate_input()` / `_security_gate_output()` (would raise
>   `TypeError`); the domain gate logic lives in module-level helpers invoked
>   inside `execute()`.
> - `GraphNode` (the `main` slot) -> deliberate no-op gate (inner-node gates
>   already applied).

### Security Controls

| Control | Where | How |
|---------|-------|-----|
| Trust gate | PreProcessNode / PostProcessNode | Both outer slots declare VERIFIED_EXTERNAL; the entry point promotes an authenticated caller |
| Input validation | PreProcessNode + ValidateInputNode | Envelope checks then the fail-closed caller-data contract (identifiers, bounds, finite numerics) |
| Input sanitization | PreProcessNode | Student e-mail / phone redaction in free text; opaque student_id preserved |
| Output gate | PostProcessNode + GenerateInterventionAlertNode | Credential + JWT + bearer + student-email scan, verbatim redaction, precision grids |
| Audit logging | every node | `emit_trace_event()` with a domain event on every invocation path |
| Secrets handling | src/api/server.py | Provisioned through the platform secrets provider; never written to State |

## Entry Point

`src/api/server.py` exposes `POST /invoke` and `GET /health`. When
`INVOKE_AUTH_TOKEN` is set, a caller that no upstream middleware vouched for
must present it as a Bearer token and is then promoted to VERIFIED_EXTERNAL;
middleware-established trust is never demoted. Auth failures return a generic
401 body. An `input_context` over 256 KiB returns 413.

## Import Isolation Confirmation
- [x] Template does not import platform-internal SDK modules
- [x] Import targets: `framework/`, `shared/`, `langgraph`, and `src.` only

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Base class | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Fixed multi-step pipeline, no autonomous reasoning loop -> Cat 2, not Cat 3 |
| Composition pattern | Flat Cat-1 slots | Inner BaseGraph via GraphNode | **Inner BaseGraph (nested)** | 5 distinct domain steps exceed the 3 backbone domain slots; nesting keeps the backbone fixed |
| input_context across the nest | Accept the framework gap | ContextVar bridge | **ContextVar bridge** | `GraphNode.execute()` does not forward input_context; without the bridge every inner read sees `{}` |
| Cohort baseline source | Hardcoded in node | Service layer | **Service layer (CohortBaselineService)** | Swappable for a real warehouse/LMS query without touching node logic |
| Missing measurement | Hard failure | Offline baseline stub | **Offline baseline stub** | Keeps the pipeline deterministic and testable without an LMS connection; the `source` field marks which path produced each score |
| Structured state storage | Bare dict fields | JSON-string fields (to_json/from_json) | **JSON-string fields** | Checkpoint msgpack safety |
| Output precision | Render only | Render + independently enforce | **Render + enforce** | A rendering change cannot silently break the documented contract |
