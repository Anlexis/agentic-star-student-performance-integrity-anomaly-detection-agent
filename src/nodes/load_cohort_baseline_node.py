"""AgentCore Platform v1.0"""

# EDU-C2-010 - LoadCohortBaselineNode
# Inner-graph domain node 2: load cohort baseline statistics for the requested
# data_type + period from the cohort baseline service.
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).
#
# The shipped CohortBaselineService is a deterministic offline stub; a
# production replacement would query the cohort warehouse / LMS analytics API
# through the platform secrets contract (per-call provider access - never
# credentials stored in State or on this node).

import logging
from typing import Any, ClassVar, Dict, List

from framework.schemas.agent_status import AgentStatus
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.cohort_baseline_service import CohortBaselineService

logger = logging.getLogger(__name__)


class LoadCohortBaselineNode(FunctionNode):
    """Load cohort baseline statistics for the requested data_type.

    Input state keys:
        anomaly_request: JSON string {student_id, data_type, period, thresholds,
                         metrics}

    Output state keys (partial dict):
        cohort_baseline: JSON string {metric: {mean, std, p25, p75, n_students}}
        node_history:    appended with a warning note if the baseline is empty
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
        data_type = str(request.get("data_type", ""))
        period = request.get("period", {}) if isinstance(request.get("period"), dict) else {}

        service = CohortBaselineService()
        try:
            baseline = service.fetch_baseline(data_type=data_type, period=period)
        except Exception as exc:  # graceful degradation - never hard-fail here
            logger.warning(
                "LoadCohortBaselineNode: baseline fetch failed for data_type=%s: %s",
                data_type,
                exc,
            )
            baseline = {}

        warnings: List[str] = []
        if not baseline:
            warnings.append(
                f"LoadCohortBaselineNode: no cohort baseline available for "
                f"data_type='{data_type}' - deviations may be incomplete"
            )

        # Domain audit: baseline loaded (metric count only - no student data).
        emit_trace_event(
            "cohort_baseline_loaded",
            {"data_type": data_type, "metric_count": len(baseline)},
            state,
        )

        logger.info(
            "LoadCohortBaselineNode: loaded %d baseline metric(s) for data_type=%s",
            len(baseline),
            data_type,
        )

        delta: Dict[str, Any] = {"cohort_baseline": to_json(baseline)}
        if warnings:
            existing_history = state.get("node_history") or []
            delta["node_history"] = list(existing_history) + warnings
        return delta
