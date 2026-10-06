# Test Specification - EDU-C2-010

## Test Strategy

- Coverage target: **>= 80%**
- Test types: Unit (per node) / Proof-of-Boundary (contracts) / End-to-end
  (through the real ASGI entry point)
- Every test is deterministic: no LLM, no network. The suite runs against the
  installed framework wheel.

| Suite | File | Tests |
|-------|------|-------|
| Unit - PreProcessNode | `tests/unit/test_pre_process_node.py` | 13 |
| Unit - ValidateInputNode (caller-data contract) | `tests/unit/test_validate_input_node.py` | 75 |
| Unit - LoadCohortBaselineNode | `tests/unit/test_load_cohort_baseline_node.py` | 8 |
| Unit - DetectDeviationsNode | `tests/unit/test_detect_deviations_node.py` | 9 |
| Unit - ClassifyAnomalyNode | `tests/unit/test_classify_anomaly_node.py` | 12 |
| Unit - GenerateInterventionAlertNode | `tests/unit/test_generate_intervention_alert_node.py` | 19 |
| Unit - PostProcessNode (output gate) | `tests/unit/test_post_process_node.py` | 40 |
| Unit - graph composition | `tests/unit/test_graph_composition.py` | 11 |
| Unit - main slot | `tests/unit/test_main_node.py` | 12 |
| PoB - input boundary | `tests/proof_of_boundary/test_validation_boundary.py` | 15 |
| PoB - credential/output boundary | `tests/proof_of_boundary/test_credential_block.py` | 16 |
| PoB - domain + state serialization | `tests/proof_of_boundary/test_domain_boundary.py` | 6 |
| PoB - invoke order | `tests/proof_of_boundary/test_pb_invoke_order.py` | 5 |
| PoB - import isolation | `tests/proof_of_boundary/test_import_isolation.py` | 1 |
| PoB - state safety (AST scan) | `tests/proof_of_boundary/test_state_safety.py` | 1 |
| PoB - end-to-end through `POST /invoke` | `tests/proof_of_boundary/test_invoke_e2e.py` | 43 |
| **Total** | | **286** |

## Framework Compliance Tests

| TC-ID | Test | Expected Result | Where | Result |
|-------|------|----------------|-------|--------|
| TC-01 | State contract: flat TypedDict | No Pydantic/dataclass; structured fields are JSON strings | `test_state_safety.py`, `test_domain_boundary.py` | Pass |
| TC-02 | Input gate rejects invalid input | PreProcessNode returns ERROR on empty / oversized / non-JSON / missing-field input | `test_validation_boundary.py` | Pass |
| TC-03 | No JWT/credential in State | Post-invoke state carries no credential-shaped token | `test_domain_boundary.py` | Pass |
| TC-04 | No credential material in node output | The baseline node's delta carries statistics only | `test_load_cohort_baseline_node.py` | Pass |
| TC-05 | No duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` absent from `execute()` bodies | `test_main_node.py` | Pass |
| TC-06 | `_security_gate_input()` not overridden | No override present (module-level helper used instead) | `test_main_node.py` | Pass |
| TC-07 | `_security_gate_output()` not overridden | No override present (module-level helper used instead) | `test_main_node.py` | Pass |
| TC-08 | `required_trust_level` declared on every node | Outer slots VERIFIED_EXTERNAL; inner nodes ANONYMOUS under the outer gate | `test_graph_composition.py` | Pass |
| TC-09 | Domain input check non-trivial | Envelope checks plus the fail-closed caller-data contract execute | `test_validate_input_node.py` | Pass |
| TC-10 | Domain output check non-trivial | Alert pre-screen + the four PostProcessNode gate layers execute | `test_post_process_node.py` | Pass |
| TC-11 | >=1 domain `emit_trace_event()` per `execute()` | Domain event emitted on every node invocation path | `test_credential_block.py` | Pass |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Test | Expected Result | Where | Result |
|-------|----------|------|----------------|-------|--------|
| PB-1 | BaseNode -> EventEmitter | `emit_trace_event()` fires on every node path | No silent failures | `test_credential_block.py` | Pass |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | `test_domain_boundary.py` | Pass |
| PB-3 | Outer -> inner graph | `input_context` crosses the nested-graph boundary and changes the outcome | Caller measurement reaches the inner pipeline | `test_invoke_e2e.py` | Pass |
| PB-4 | Import isolation | No platform-internal imports | AST scan: 0 violations | `test_import_isolation.py` | Pass |
| PB-5 | Checkpoint safety | No JWT/Pydantic in checkpointed fields | AST scan pass | `test_state_safety.py` | Pass |
| PB-6 | Invoke execution order | trust gate -> input gate -> execute() -> output gate, in backbone order | Order verified | `test_pb_invoke_order.py` | Pass |
| PB-7 | Entry-point auth | Missing/wrong Bearer token is refused; oversized context is refused | 401 / 413, generic body | `test_invoke_e2e.py` | Pass |

## Caller-Data Contract Tests (fail-closed)

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| CD-01 | Opaque identifier enforced | `student_id` over-length / whitespace / e-mail-shaped / wrong type | ERROR naming `student_id`; value never echoed | Pass |
| CD-02 | data_type vocabulary enforced | unsupported `data_type` | ERROR; value never echoed | Pass |
| CD-03 | Period format enforced | `"04-2025"`, `"last semester"`, `"2025/04/01"` | ERROR naming `period.start` | Pass |
| CD-04 | Threshold finite + bounded | `NaN` / `Infinity` / `-Infinity` (parsed and raw JSON), bool, numeric string, 0, 11.0 | ERROR naming `thresholds.z_score` | Pass |
| CD-05 | Metric value finite + bounded, per field | non-finite matrix + over/under bounds, for all four metrics | ERROR naming `metrics.<field>` | Pass |
| CD-06 | Metric name locked to the data_type | unknown name / wrong data_type's metric / non-identifier | ERROR; name never echoed | Pass |
| CD-07 | Structural caps | more than 8 metric entries; non-dict metrics block | ERROR | Pass |
| CD-08 | Context channel fails closed | malformed `input_context.metrics` | ERROR - never a silent fallback to the payload channel | Pass |
| CD-09 | Bounds are inclusive | `attendance_rate = 0.0` | Accepted | Pass |
| CD-10 | Operator config degrades, caller data does not | malformed `thresholds_config` value | Falls back to the documented default, no error | Pass |

## Output-Contract Tests

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| OC-01 | Credential-shaped token blocked | ERROR + sanitised stub in both `formatted_output` and `result` | Pass |
| OC-02 | Student e-mail blocked | ERROR + sanitised stub | Pass |
| OC-03 | Verbatim caller text redacted | `[REDACTED]` replaces the embedding; short overlaps untouched | Pass |
| OC-04 | Decimal grid enforced | 3+-fraction-digit tokens snap to 2 decimals; on-grid tokens byte-identical | Pass |
| OC-05 | Raw-magnitude grid enforced | comma-grouped, 5+-digit runs, and currency-marked values (either order, attached or any whitespace run, signed) snap to the 1,000 grid | Pass |
| OC-06 | Structural tokens preserved | years, bare counts, horizons, 2-decimal statistics byte-identical | Pass |
| OC-07 | Raw measured value never rendered | The caller's measurement drives the computation but never appears in the alert | Pass |
| OC-08 | Schema note always present | Every alert carries the precision contract note | Pass |

## Business Logic Tests

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | Anomaly detected: grade drop | `grade_delta = -40.0` (z = -5.00) | anomaly_type=grade_drop, confidence 1.00, severity HIGH | Pass |
| BL-02 | No anomaly: within baseline | `grade_delta = 0.5` | anomaly_type=no_anomaly, action "No intervention required", severity LOW | Pass |
| BL-03 | Multi-signal anomaly | two flagged metrics | anomaly_type=multi_signal, confidence = mean of contributions | Pass |
| BL-04 | Missing cohort baseline | unknown data_type at the service | empty baseline, warning appended to node_history, no hard fail | Pass |
| BL-05 | Academic integrity flag | `similarity_score = 0.95` | anomaly_type=similarity_flag, action "Refer to academic integrity board" | Pass |
| BL-06 | Invalid input | missing `student_id` | ERROR at pre_process, no downstream execution | Pass |
| BL-07 | Every data_type reaches its anomaly type | one measurement per data_type | grade_drop / attendance_gap / submission_timing / similarity_flag | Pass |
| BL-08 | Severity bands reachable | HIGH and MEDIUM at the default flag threshold; LOW with a lowered flag threshold | Band asserted per case | Pass |
| BL-09 | Config bands govern severity | shifted `confidence_high` / `confidence_medium` | Severity follows the config, not hardcoded constants | Pass |
| BL-10 | Absent measurement degrades | no metrics supplied | Offline baseline stub scores the metric; `source` marks the path | Pass |

## Severity / mapping reference (asserted by the BL cases)

| anomaly_type | recommended_action | severity rule |
|--------------|--------------------|---------------|
| grade_drop | Schedule academic support meeting | by confidence band |
| attendance_gap | Trigger attendance outreach | by confidence band |
| submission_timing | Flag for academic integrity review | by confidence band |
| similarity_flag | Refer to academic integrity board | by confidence band |
| multi_signal | Escalate to counsellor + academic integrity review | by confidence band |
| no_anomaly | No intervention required | LOW |

Confidence -> severity bands (defaults): `< 0.4` LOW, `0.4 <= c < 0.7` MEDIUM,
`>= 0.7` HIGH. The live bands come from `config/config.yaml`.

> Note: at the default flag threshold (`|z| >= 2.0`) a *flagged* anomaly always
> scores at least 0.5 confidence, so it is never LOW. The LOW band applies to
> the no-anomaly outcome, and to flagged anomalies when the caller lowers the
> flag threshold.

## Test Execution Summary

- Total tests: **286**
- Pass / Fail / Skip: **286 / 0 / 0**
- Command: `python -m pytest tests/ -v`
