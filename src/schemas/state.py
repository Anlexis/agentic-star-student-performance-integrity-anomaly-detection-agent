"""AgentCore Platform v1.0"""

# State must be a flat TypedDict - never Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization; Pydantic objects cause silent
# corruption.  Extend AgentState with agent-specific fields only.  Do NOT add
# credentials, secrets, or Pydantic models.
#
# [!] msgpack safety: structured fields (dict / list[dict]) are stored as JSON
# STRINGS, not bare Python containers - a bare dict/list in a checkpointed
# State field breaks checkpoint serialization guarantees. Producers serialize
# with to_json() on write; consumers deserialize with from_json() on read.
#
# EDU-C2-010 - StudentPerformanceAnomalyDetectionAgent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# FERPA / student-data note: student personal data (names, e-mail, raw
# identifiers beyond the opaque student_id) is surface-stripped by
# PreProcessNode and never persisted to the checkpoint DB.  Downstream domain
# nodes operate on the opaque student_id, the caller's validated measurements,
# and cohort-relative statistics only - never raw PII.

import json
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a dict/list State field to a JSON string (msgpack safety).

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string State field back to its dict/list.

    None / empty / malformed input -> the supplied ``default`` so a missing or
    corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for EDU-C2-010.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.

    Convention: structured fields (anomaly_request, thresholds_config,
    cohort_baseline, deviation_scores, intervention_alert) are stored as JSON
    STRINGS via to_json() and read back via from_json() (msgpack safety).
    Scalar fields (validated_input, anomaly_type, anomaly_confidence, result)
    are stored as their native primitive type.
    """

    # ------------------------------------------------------------------
    # Outer layer - set by PreProcessNode / DomainWorkflowGraphNode.merge_output
    # ------------------------------------------------------------------

    # PII-surface-stripped request text produced by PreProcessNode.
    # Raw user_input is NOT persisted beyond PreProcessNode.
    validated_input: Optional[str]

    # JSON STRING (to_json) of the structured anomaly-detection request, parsed
    # and normalised by PreProcessNode / ValidateInputNode. Deserialised shape:
    # {"student_id": str, "data_type": str, "period": {"start": str, "end": str},
    #  "thresholds": {"z_score": float}, "metrics": {metric: float}}
    # Consumers (LoadCohortBaselineNode / DetectDeviationsNode) read via from_json().
    anomaly_request: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer - domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # Seeded by DomainWorkflowGraph._extra_initial_state().
    # JSON STRING (to_json) of the config/config.yaml anomaly_thresholds block:
    # {"z_score_threshold": float, "confidence_high": float,
    #  "confidence_medium": float}
    # Consumers (ValidateInputNode / GenerateInterventionAlertNode) read it via
    # from_json() with documented module fallbacks.
    thresholds_config: Optional[str]

    # LoadCohortBaselineNode output
    # JSON STRING (to_json) of the cohort baseline statistics, keyed by metric.
    # Deserialised dict shape:
    # {metric: {"mean": float, "std": float, "p25": float, "p75": float,
    #           "n_students": int}}
    # Consumer (DetectDeviationsNode) reads it via from_json().
    cohort_baseline: Optional[str]

    # DetectDeviationsNode output
    # JSON STRING (to_json) of the per-metric deviation scores comparing the
    # student against the cohort baseline. Deserialised dict shape:
    # {metric: {"z_score": float, "percentile": float, "is_anomalous": bool,
    #           "source": "measured" | "baseline_stub"}}
    # Consumer (ClassifyAnomalyNode) reads it via from_json().
    deviation_scores: Optional[str]

    # ClassifyAnomalyNode outputs (scalars - stored natively)
    # Primary anomaly type, e.g. 'grade_drop', 'attendance_gap',
    # 'submission_timing', 'similarity_flag', 'multi_signal', 'no_anomaly'.
    anomaly_type: Optional[str]
    # Overall classification confidence in [0.0, 1.0].
    anomaly_confidence: Optional[float]

    # GenerateInterventionAlertNode output
    # JSON STRING (to_json) of the intervention alert. Deserialised dict shape:
    # {"anomaly_type": str, "confidence": float, "severity": str,
    #  "recommended_action": str, "evidence_summary": [str], "data_type": str,
    #  "schema_note": str}
    # Consumer (PostProcessNode, via the outer `result` mapping) reads it.
    intervention_alert: Optional[str]

    # Serialised final result for response_metadata (set by
    # GenerateInterventionAlertNode; surfaced to the caller by PostProcessNode).
    result: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit - framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    error_code: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
