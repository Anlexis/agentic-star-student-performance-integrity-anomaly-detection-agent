"""AgentCore Platform v1.0"""

# EDU-C2-010 - ValidateInputNode
# Inner-graph domain node 1: the caller-data contract. Deep structural
# validation + normalisation of the anomaly-detection request produced by the
# outer PreProcessNode, plus fail-closed validation of every caller-controlled
# field - identifier patterns, period format, and finite+bounded numeric
# checks on the threshold override and every measured metric.
#
# Fail-closed policy: a request that violates the contract terminates the run
# with ERROR and a field-NAMING error message - the offending value is never
# echoed into errors or logs. NaN / Infinity deserve special care: they parse
# via float() AND arrive intact through raw JSON, and IEEE comparisons against
# NaN are always False - an unchecked NaN threshold would silently suppress
# every alert (fail-open on the exact decision this template exists for).
#
# Wired by the inner graph (DomainWorkflowGraph).
# Returns only changed state keys (partial dict).
#
# Input resolution order for the structured request:
#   1. anomaly_request (JSON string) - set when running inside the outer graph
#      after PreProcessNode, i.e. the full nested Cat-2 pipeline.
#   2. user_input (JSON string) - used when the inner graph is invoked via
#      GraphNode.extract_input(), which passes validated_input as the inner
#      graph's user_input. In that path anomaly_request is absent from the
#      inner initial_state because BaseGraph.invoke() starts with a clean slate
#      that only carries user_input (not the outer state's anomaly_request).

import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

_ALLOWED_DATA_TYPES = {"grades", "attendance", "submission", "similarity"}

# The measured metric each data_type is scored on - the same keys the cohort
# baseline is published under (src/services/cohort_baseline_service.py).
_DATA_TYPE_TO_METRIC: Dict[str, str] = {
    "grades": "grade_delta",
    "attendance": "attendance_rate",
    "submission": "submission_timing_offset",
    "similarity": "similarity_score",
}

# Explicit per-metric bounds for caller-supplied measurements. Values outside
# these ranges are physically implausible for the metric and are rejected.
_METRIC_BOUNDS: Dict[str, Tuple[float, float]] = {
    "grade_delta": (-100.0, 100.0),  # grade-point change vs. prior period
    "attendance_rate": (0.0, 1.0),  # fraction of sessions attended
    "submission_timing_offset": (-720.0, 720.0),  # hours vs. deadline (30 days)
    "similarity_score": (0.0, 1.0),  # similarity fraction
}

# Bounds for the per-request z-score threshold override.
_Z_THRESHOLD_MIN = 0.5
_Z_THRESHOLD_MAX = 10.0
_DEFAULT_Z_THRESHOLD = 2.0

# Structural cap on the caller metrics block - the contract defines exactly
# one metric per data_type, so anything beyond a handful of entries is not a
# legitimate request shape.
_MAX_METRIC_ENTRIES = 8

# Caller identifiers: opaque id token, bounded length, no free text.
_STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Metric names must be inert identifiers even before the known-set check.
_INERT_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Period bounds: empty (open window) or ISO-style YYYY-MM / YYYY-MM-DD.
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _finite_in_range(value: Any, low: float, high: float) -> Optional[float]:
    """Parse an untrusted caller numeric, fail-closed.

    Accepts only a real int/float (bool excluded) that is FINITE and within
    [low, high]. Returns the float, or None on any violation. Strings are
    rejected outright (numeric strings included): the request contract is
    JSON numbers. NaN / +-Infinity are rejected explicitly - both parse fine
    via float() and arrive intact through raw JSON, and NaN comparisons are
    always False, which would otherwise fail silently OPEN.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if not low <= number <= high:
        return None
    return number


def _config_float(config: Dict[str, Any], key: str, low: float, high: float, default: float) -> float:
    """Read an operator-config float with a shape guard.

    Config values are operator-controlled (config/config.yaml), not
    caller-controlled - a malformed value degrades to the documented module
    default instead of failing the request.
    """
    parsed = _finite_in_range(config.get(key), low, high)
    return parsed if parsed is not None else default


class ValidateInputNode(FunctionNode):
    """Deep-validate and normalise the anomaly request (fail-closed).

    Input state keys (resolved in order):
        anomaly_request:  JSON string of the structured request (from
                          PreProcessNode when running inside the outer graph).
        user_input:       Fallback - the validated_input JSON string passed by
                          DomainWorkflowGraphNode.extract_input() as the inner
                          graph's user_input when anomaly_request is absent.
        input_context:    Structured invocation data bridged from the outer
                          graph (src/graph/context_bridge.py). The caller's
                          measured `metrics` block is read from here first;
                          the request payload's `metrics` block is the
                          fallback channel.
        thresholds_config: JSON string of the config/config.yaml
                          anomaly_thresholds block, seeded by
                          DomainWorkflowGraph._extra_initial_state().

    Output state keys (partial dict):
        validated_input: confirmed-valid request echo (unchanged or re-affirmed)
        anomaly_request: normalised request (z_score threshold resolved, period
                         bounds verified, metrics validated finite+bounded)
        status / error_log: AgentStatus.ERROR on any contract violation -
                         one field-naming message per violation, values never
                         echoed
    """

    # Trust is enforced once at the outer entry gate (pre_process slot,
    # VERIFIED_EXTERNAL). The inner graph runs under the same caller context,
    # so inner nodes declare ANONYMOUS rather than re-raising the bar
    # mid-pipeline.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # Resolution order: anomaly_request (outer-graph path) -> user_input
        # (inner-graph standalone path via GraphNode.extract_input()).
        request: Dict[str, Any] = from_json(state.get("anomaly_request"), {})
        if not request or not isinstance(request, dict):
            request = from_json(state.get("user_input", ""), {})

        if not request or not isinstance(request, dict):
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["ValidateInputNode: anomaly_request is missing or malformed"],
            }

        errors: List[str] = []
        notes: List[str] = []

        # -- Required identity fields ------------------------------------
        missing_keys = {"student_id", "data_type"} - request.keys()
        if missing_keys:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"ValidateInputNode: missing required keys {sorted(missing_keys)} in anomaly_request"],
            }

        student_id = request.get("student_id")
        if not isinstance(student_id, str) or not _STUDENT_ID_RE.match(student_id):
            errors.append(
                "ValidateInputNode: student_id must be an opaque identifier "
                "(letters, digits, '-' or '_', at most 64 characters)"
            )
            student_id = ""

        data_type = request.get("data_type")
        if data_type not in _ALLOWED_DATA_TYPES:
            # data_type is caller-controlled free text at this point - never
            # echo it; the allowed set names the contract.
            emit_progress(INVALID_VALUE)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"ValidateInputNode: unsupported data_type (allowed: {sorted(_ALLOWED_DATA_TYPES)})"],
            }

        # -- Period window -----------------------------------------------
        period = request.get("period", {})
        if not isinstance(period, dict):
            period = {}
        normalised_period: Dict[str, str] = {}
        for bound in ("start", "end"):
            raw_bound = str(period.get(bound, "")).strip()
            if raw_bound and not _PERIOD_RE.match(raw_bound):
                errors.append(f"ValidateInputNode: period.{bound} must be an ISO date (YYYY-MM or YYYY-MM-DD) or empty")
                raw_bound = ""
            normalised_period[bound] = raw_bound
        if not normalised_period["start"] or not normalised_period["end"]:
            notes.append("period bounds incomplete - baseline uses full-history window")

        # -- z-score threshold (config default, caller override) ---------
        thresholds_config: Dict[str, Any] = from_json(state.get("thresholds_config"), {})
        if not isinstance(thresholds_config, dict):
            thresholds_config = {}
        z_threshold = _config_float(
            thresholds_config,
            "z_score_threshold",
            _Z_THRESHOLD_MIN,
            _Z_THRESHOLD_MAX,
            _DEFAULT_Z_THRESHOLD,
        )

        thresholds = request.get("thresholds", {})
        if not isinstance(thresholds, dict):
            thresholds = {}
        if "z_score" in thresholds:
            override = _finite_in_range(thresholds.get("z_score"), _Z_THRESHOLD_MIN, _Z_THRESHOLD_MAX)
            if override is None:
                errors.append(
                    "ValidateInputNode: thresholds.z_score must be a finite number "
                    f"between {_Z_THRESHOLD_MIN} and {_Z_THRESHOLD_MAX}"
                )
            else:
                z_threshold = override

        # -- Measured metrics (input_context first, payload fallback) ----
        input_context = state.get("input_context", {})
        if not isinstance(input_context, dict):
            input_context = {}
        if "metrics" in input_context:
            metrics_supplied: Any = input_context.get("metrics")
            metrics_channel = "input_context"
        else:
            metrics_supplied = request.get("metrics", {})
            metrics_channel = "request"

        expected_metric = _DATA_TYPE_TO_METRIC[data_type]
        validated_metrics: Dict[str, float] = {}
        if metrics_supplied in (None, {}):
            notes.append("no measured metrics supplied - deviations use the offline baseline stub")
        elif not isinstance(metrics_supplied, dict):
            errors.append(f"ValidateInputNode: metrics ({metrics_channel}) must be a JSON object")
        elif len(metrics_supplied) > _MAX_METRIC_ENTRIES:
            errors.append(f"ValidateInputNode: metrics ({metrics_channel}) exceeds {_MAX_METRIC_ENTRIES} entries")
        else:
            for metric_name, metric_value in metrics_supplied.items():
                if (
                    not isinstance(metric_name, str)
                    or not _INERT_IDENTIFIER_RE.match(metric_name)
                    or metric_name != expected_metric
                ):
                    # The metric name is caller-controlled - never echo it.
                    errors.append(
                        f"ValidateInputNode: metrics ({metrics_channel}) contains an "
                        f"unsupported metric name for data_type '{data_type}' "
                        f"(expected: '{expected_metric}')"
                    )
                    continue
                low, high = _METRIC_BOUNDS[metric_name]
                parsed = _finite_in_range(metric_value, low, high)
                if parsed is None:
                    errors.append(
                        f"ValidateInputNode: metrics.{metric_name} must be a finite " f"number between {low} and {high}"
                    )
                    continue
                validated_metrics[metric_name] = parsed

        if errors:
            # Fail CLOSED: any contract violation terminates the run - the
            # pipeline never proceeds on a partially-validated request.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": errors,
            }

        normalised_request: Dict[str, Any] = {
            "student_id": student_id,
            "data_type": data_type,
            "period": normalised_period,
            "thresholds": {"z_score": z_threshold},
            "metrics": validated_metrics,
        }

        # Domain audit: the request passed deep validation (counts only - no
        # student data).
        emit_trace_event(
            "validate_input_complete",
            {
                "data_type": data_type,
                "z_threshold": z_threshold,
                "metric_count": len(validated_metrics),
                "note_count": len(notes),
            },
            state,
        )

        logger.info(
            "ValidateInputNode: validated data_type=%s, z_threshold=%.2f, metrics=%d",
            data_type,
            z_threshold,
            len(validated_metrics),
        )

        return {
            "validated_input": state.get("validated_input") or state.get("user_input", ""),
            "anomaly_request": to_json(normalised_request),
        }
