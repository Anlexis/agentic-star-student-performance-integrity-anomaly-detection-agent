"""AgentCore Platform v1.0"""

# EDU-C2-010 - GenerateInterventionAlertNode
# Inner-graph domain node 5 (terminal): map the classified anomaly to a
# recommended intervention, compute severity, build an evidence summary, and
# emit the final intervention alert + serialised result.
#
# External output schema (enforced again, independently, by the outer
# PostProcessNode gate): the alert renders cohort-relative AGGREGATES only -
# z-scores, percentiles and confidence, each rounded to 2 decimal places. Raw
# measured values are never rendered (they are dropped upstream, in
# DetectDeviationsNode). The alert carries a schema note stating this
# contract.
#
# This node also runs an output-content pre-screen over the assembled alert
# (module-level helper, invoked inside execute) before serialising it - the
# authoritative outer gate is PostProcessNode. We do NOT override
# FunctionNode._security_gate_output() (it is @final - TypeError).
#
# Wired by the inner graph (DomainWorkflowGraph) as the terminal node.
# Returns only changed state keys (partial dict).

import json
import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# anomaly_type -> recommended action (consistent with ClassifyAnomalyNode).
_RECOMMENDED_ACTION: Dict[str, str] = {
    "grade_drop": "Schedule academic support meeting",
    "attendance_gap": "Trigger attendance outreach",
    "submission_timing": "Flag for academic integrity review",
    "similarity_flag": "Refer to academic integrity board",
    "multi_signal": "Escalate to counsellor + academic integrity review",
    "no_anomaly": "No intervention required",
}

# Confidence -> severity band defaults (<0.4 LOW, <0.7 MEDIUM, >=0.7 HIGH).
# The live values come from config/config.yaml `anomaly_thresholds`
# (confidence_high / confidence_medium), read via the state-seeded
# thresholds_config field; these module constants are the documented fallback.
_SEVERITY_MEDIUM_AT = 0.4
_SEVERITY_HIGH_AT = 0.7

# Schema note rendered inside every alert - the external precision contract
# (enforced independently by the PostProcessNode output gate).
_SCHEMA_NOTE = (
    "Statistics are cohort-relative aggregates rounded to 2 decimal places; " "raw measured values are not included."
)

# Output pre-screen patterns: credential-like strings + student e-mail PII
# that must never be embedded in a returned alert.
_SENSITIVE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("student_email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]


def _extra_output_prescreen(alert: Dict[str, Any]) -> Optional[str]:
    """Extra output pre-screen: scan the alert for credentials / student PII.

    Returns the name of the first matched violation, or None if clean.
    Implemented as a module-level helper (NOT a FunctionNode method override -
    _security_gate_output is @final).
    """
    blob = json.dumps(alert, ensure_ascii=False)
    for name, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(blob):
            return name
    return None


def _severity_bands(state: AgentState) -> Tuple[float, float]:
    """Resolve the (medium_at, high_at) confidence bands.

    Reads the config-seeded thresholds_config state field; a malformed or
    missing value degrades to the documented module defaults. Guards: both
    bands finite in (0, 1], and medium strictly below high.
    """
    config: Dict[str, Any] = from_json(state.get("thresholds_config"), {})
    if not isinstance(config, dict):
        config = {}

    def _band(key: str, default: float) -> float:
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        number = float(value)
        if not math.isfinite(number) or not 0.0 < number <= 1.0:
            return default
        return number

    medium_at = _band("confidence_medium", _SEVERITY_MEDIUM_AT)
    high_at = _band("confidence_high", _SEVERITY_HIGH_AT)
    if medium_at >= high_at:
        return _SEVERITY_MEDIUM_AT, _SEVERITY_HIGH_AT
    return medium_at, high_at


def _severity_for(confidence: float, medium_at: float, high_at: float) -> str:
    """Map a confidence score to a LOW / MEDIUM / HIGH severity band."""
    if confidence >= high_at:
        return "HIGH"
    if confidence >= medium_at:
        return "MEDIUM"
    return "LOW"


def _build_evidence_summary(deviation_scores: Dict[str, Any]) -> List[str]:
    """List the flagged metrics as cohort-relative aggregates (no raw values).

    Each line renders the z-score and percentile rounded to 2 decimal places -
    the external precision contract. Raw measured values never appear here.
    """
    summary: List[str] = []
    for metric, scores in deviation_scores.items():
        if isinstance(scores, dict) and scores.get("is_anomalous"):
            z_score = float(scores.get("z_score", 0.0))
            percentile = float(scores.get("percentile", 0.0))
            summary.append(f"{metric}: z={z_score:.2f}, percentile={percentile:.2f}")
    return summary


class GenerateInterventionAlertNode(FunctionNode):
    """Build the intervention alert from the classified anomaly.

    Input state keys:
        anomaly_type:       str (from ClassifyAnomalyNode)
        anomaly_confidence: float (from ClassifyAnomalyNode)
        deviation_scores:   JSON string (from DetectDeviationsNode)
        anomaly_request:    JSON string (for data_type context)
        thresholds_config:  JSON string (config severity bands)

    Output state keys (partial dict):
        intervention_alert: JSON string
            {anomaly_type, confidence, severity, recommended_action,
             evidence_summary, data_type, schema_note}
        result:             JSON string (serialised alert for response_metadata)
        status:             AgentStatus.SUCCESS / ERROR (on pre-screen hit)
    """

    # Trust is enforced once at the outer entry gate (pre_process slot,
    # VERIFIED_EXTERNAL); inner nodes run under the same caller context.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        anomaly_type = str(state.get("anomaly_type", "no_anomaly"))
        confidence = float(state.get("anomaly_confidence", 0.0) or 0.0)
        deviation_scores: Dict[str, Any] = from_json(state.get("deviation_scores"), {})
        request: Dict[str, Any] = from_json(state.get("anomaly_request"), {})

        recommended_action = _RECOMMENDED_ACTION.get(anomaly_type, _RECOMMENDED_ACTION["no_anomaly"])
        medium_at, high_at = _severity_bands(state)
        severity = "LOW" if anomaly_type == "no_anomaly" else _severity_for(confidence, medium_at, high_at)
        evidence_summary = _build_evidence_summary(deviation_scores)

        intervention_alert: Dict[str, Any] = {
            "anomaly_type": anomaly_type,
            "confidence": round(confidence, 2),
            "severity": severity,
            "recommended_action": recommended_action,
            "evidence_summary": evidence_summary,
            "data_type": str(request.get("data_type", "")),
            "schema_note": _SCHEMA_NOTE,
        }

        # Output pre-screen: refuse to emit an alert carrying credentials/PII.
        violation = _extra_output_prescreen(intervention_alert)
        if violation:
            logger.error(
                "GenerateInterventionAlertNode: alert blocked - violation: %s",
                violation,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "GenerateInterventionAlertNode: intervention alert blocked - "
                    f"disallowed content detected ({violation})"
                ],
            }

        result = to_json(intervention_alert)

        # Domain audit: an intervention alert was generated.
        emit_trace_event(
            "intervention_alert_generated",
            {
                "anomaly_type": anomaly_type,
                "severity": severity,
                "confidence": round(confidence, 2),
            },
            state,
        )

        logger.info(
            "GenerateInterventionAlertNode: type=%s severity=%s action='%s'",
            anomaly_type,
            severity,
            recommended_action,
        )

        return {
            "intervention_alert": result,
            "result": result,
            "status": AgentStatus.SUCCESS.value,
        }
