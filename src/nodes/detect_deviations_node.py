"""AgentCore Platform v1.0"""

# EDU-C2-010 - DetectDeviationsNode
# Inner-graph domain node 3: compute per-metric deviation scores comparing the
# student's measured values against the cohort baseline (z-score / percentile /
# anomaly flag).
#
# Pure computation - no external service calls.
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).
#
# Data source per metric:
#   measured      - the caller supplied the measurement (validated finite and
#                   in range by ValidateInputNode); the score is computed from
#                   the real value.
#   baseline_stub - no measurement was supplied; the offline stub derives a
#                   stable pseudo-value from the opaque student_id so the
#                   pipeline stays deterministic and testable without an LMS
#                   connection. A production deployment would read the real
#                   measurement from the LMS instead.
#
# The raw measured value is used for the computation only - it is never
# carried forward in deviation_scores (the alert renders cohort-relative
# aggregates, not raw measurements).

import logging
import math
from typing import Any, ClassVar, Dict

from framework.schemas.agent_status import AgentStatus
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

_DEFAULT_Z_THRESHOLD = 2.0


def _normal_cdf(z: float) -> float:
    """Standard-normal CDF -> percentile in [0, 1] (deterministic, stdlib only)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _stub_value(metric: str, student_id: str, baseline: Dict[str, Any]) -> float:
    """Derive the offline-stub value for a metric when no measurement was supplied.

    Deterministic signed offset in roughly [-3.5, +3.5] std around the cohort
    mean, stable per student - keeps the pipeline testable offline. A
    production node would read the real measured value instead.
    """
    mean = float(baseline.get("mean", 0.0))
    std = float(baseline.get("std", 1.0)) or 1.0
    seed = sum(ord(c) for c in f"{student_id}:{metric}")
    offset_steps = (seed % 71) / 10.0 - 3.5  # -3.5 .. +3.5
    return mean + offset_steps * std


class DetectDeviationsNode(FunctionNode):
    """Compute per-metric deviation scores vs. the cohort baseline.

    Input state keys:
        anomaly_request: JSON string {student_id, data_type, period,
                         thresholds, metrics} - metrics already validated
                         finite+bounded by ValidateInputNode
        cohort_baseline: JSON string {metric: {mean, std, p25, p75, n_students}}

    Output state keys (partial dict):
        deviation_scores: JSON string
            {metric: {z_score, percentile, is_anomalous, source}}
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
        request: Dict[str, Any] = from_json(state.get("anomaly_request"), {})
        baseline: Dict[str, Any] = from_json(state.get("cohort_baseline"), {})

        student_id = str(request.get("student_id", ""))
        thresholds = request.get("thresholds", {})
        if not isinstance(thresholds, dict):
            thresholds = {}
        z_threshold_raw = thresholds.get("z_score", _DEFAULT_Z_THRESHOLD)
        z_threshold = (
            float(z_threshold_raw)
            if isinstance(z_threshold_raw, (int, float)) and not isinstance(z_threshold_raw, bool)
            else _DEFAULT_Z_THRESHOLD
        )
        if not math.isfinite(z_threshold) or z_threshold <= 0:
            z_threshold = _DEFAULT_Z_THRESHOLD

        # Measured metrics: validated finite+bounded upstream (ValidateInputNode).
        supplied_metrics = request.get("metrics", {})
        if not isinstance(supplied_metrics, dict):
            supplied_metrics = {}

        deviation_scores: Dict[str, Any] = {}
        measured_count = 0
        for metric, stats in baseline.items():
            if not isinstance(stats, dict):
                # Missing / malformed baseline metric - skip gracefully.
                logger.warning(
                    "DetectDeviationsNode: baseline metric '%s' malformed - skipped",
                    metric,
                )
                continue

            mean = float(stats.get("mean", 0.0))
            std = float(stats.get("std", 1.0)) or 1.0

            supplied = supplied_metrics.get(metric)
            if isinstance(supplied, (int, float)) and not isinstance(supplied, bool) and math.isfinite(float(supplied)):
                value = float(supplied)
                source = "measured"
                measured_count += 1
            else:
                value = _stub_value(metric, student_id, stats)
                source = "baseline_stub"

            z_score = (value - mean) / std
            percentile = _normal_cdf(z_score)
            is_anomalous = abs(z_score) >= z_threshold

            deviation_scores[metric] = {
                "z_score": round(z_score, 4),
                "percentile": round(percentile, 4),
                "is_anomalous": bool(is_anomalous),
                "source": source,
            }

        anomalous_count = sum(1 for m in deviation_scores.values() if m.get("is_anomalous"))

        # Domain audit: deviations computed (counts only - no student data).
        emit_trace_event(
            "deviations_computed",
            {
                "metric_count": len(deviation_scores),
                "measured_count": measured_count,
                "anomalous_count": anomalous_count,
                "z_threshold": z_threshold,
            },
            state,
        )

        logger.info(
            "DetectDeviationsNode: %d metric(s), %d measured, %d anomalous (z>=%.2f)",
            len(deviation_scores),
            measured_count,
            anomalous_count,
            z_threshold,
        )

        return {
            "deviation_scores": to_json(deviation_scores),
        }
