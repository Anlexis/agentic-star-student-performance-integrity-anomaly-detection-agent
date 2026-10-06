"""AgentCore Platform v1.0"""

# EDU-C2-010 - ClassifyAnomalyNode
# Inner-graph domain node 4: determine the primary anomaly type + overall
# confidence from the per-metric deviation scores.
#
# Pure classification logic - no external service calls.
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).

import logging
from typing import Any, ClassVar, Dict, List

from framework.schemas.agent_status import AgentStatus
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Map a baseline metric key to the anomaly_type it raises when flagged.
# Kept consistent with GenerateInterventionAlertNode's intervention mapping.
_METRIC_TO_ANOMALY_TYPE: Dict[str, str] = {
    "grade_delta": "grade_drop",
    "attendance_rate": "attendance_gap",
    "submission_timing_offset": "submission_timing",
    "similarity_score": "similarity_flag",
}

# Normalisation ceiling: a |z| at or above this maps to confidence ~1.0.
_Z_CONFIDENCE_CEILING = 4.0


def _z_to_confidence(abs_z: float) -> float:
    """Normalise an absolute z-score to a [0, 1] confidence contribution."""
    return max(0.0, min(1.0, abs_z / _Z_CONFIDENCE_CEILING))


class ClassifyAnomalyNode(FunctionNode):
    """Classify the primary anomaly type and overall confidence.

    Input state keys:
        deviation_scores: JSON string
            {metric: {z_score, percentile, is_anomalous, source}}

    Output state keys (partial dict):
        anomaly_type:       str (grade_drop / attendance_gap / submission_timing /
                            similarity_flag / multi_signal / no_anomaly)
        anomaly_confidence: float in [0.0, 1.0]
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
        deviation_scores: Dict[str, Any] = from_json(state.get("deviation_scores"), {})

        flagged: List[str] = []
        contributions: List[float] = []
        for metric, scores in deviation_scores.items():
            if not isinstance(scores, dict):
                continue
            if scores.get("is_anomalous"):
                flagged.append(metric)
                contributions.append(_z_to_confidence(abs(float(scores.get("z_score", 0.0)))))

        if not flagged:
            anomaly_type = "no_anomaly"
            anomaly_confidence = 0.0
        elif len(flagged) >= 2:
            anomaly_type = "multi_signal"
            # Weighted (mean) of the triggered metric confidences.
            anomaly_confidence = sum(contributions) / len(contributions)
        else:
            metric = flagged[0]
            anomaly_type = _METRIC_TO_ANOMALY_TYPE.get(metric, "grade_drop")
            anomaly_confidence = contributions[0]

        anomaly_confidence = round(max(0.0, min(1.0, anomaly_confidence)), 4)

        # Domain audit: an anomaly classification was produced.
        emit_trace_event(
            "anomaly_classified",
            {
                "anomaly_type": anomaly_type,
                "anomaly_confidence": anomaly_confidence,
                "flagged_metrics": flagged,
            },
            state,
        )

        logger.info(
            "ClassifyAnomalyNode: type=%s confidence=%.4f (%d flagged)",
            anomaly_type,
            anomaly_confidence,
            len(flagged),
        )

        return {
            "anomaly_type": anomaly_type,
            "anomaly_confidence": anomaly_confidence,
        }
