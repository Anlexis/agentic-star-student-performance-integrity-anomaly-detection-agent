"""AgentCore Platform v1.0"""

# EDU-C2-010 - PreProcessNode (outer pre_process slot; input validation)
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants - never plain strings
#  - Read input_context via state.get("input_context", {}) - read-only
#  - Never import from mediator/, api/, or other agents
#
# Parse + validate the JSON anomaly-detection request and reject empty /
# oversized / malformed / unsupported input before the inner domain workflow
# runs. FERPA: a light surface PII pre-strip happens here (names / e-mail /
# phone in free text); the opaque student_id is preserved (it is the
# cohort-relative key, not raw PII). Raw input is never persisted to the
# checkpoint DB.
#
# Do NOT override _security_gate_input() (FunctionNode @final - TypeError).
# The surface PII strip is implemented as a module-level helper invoked
# inside execute().

import json
import re
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, TOO_LONG
from src.schemas.state import to_json

# data_type values accepted by the anomaly pipeline.
_ALLOWED_DATA_TYPES = {"grades", "attendance", "submission", "similarity"}

# Hard cap on the raw request size (defence-in-depth on input size). A
# legitimate anomaly request (ids + one metrics block) is well under 1 KiB.
_MAX_INPUT_CHARS = 16_384

# Caller strings that are used as routing/telemetry values must be inert
# identifiers - lowercase alphanumerics/underscore, bounded length. Free text
# in such a field is caller-controlled log injection and is replaced with a
# fixed fallback before use.
_INERT_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Surface-level student-PII patterns redacted from free-text values before
# validated_input is written. The opaque student_id (e.g. "STU-12345") is NOT
# redacted - it is the cohort-relative key, not raw personal data.
_PII_PATTERNS: List[re.Pattern[str]] = [
    # E-mail addresses.
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    # Phone numbers (loose): 0XX-XXXX-XXXX / +.. groups.
    re.compile(r"\b\+?\d[\d -]{7,}\d\b"),
]
_PII_REPLACEMENT = "[REDACTED]"


def _surface_strip_pii(text: str) -> str:
    """Redact obvious free-text student-PII tokens from a string (FERPA)."""
    for pattern in _PII_PATTERNS:
        text = pattern.sub(_PII_REPLACEMENT, text)
    return text


class PreProcessNode(FunctionNode):
    """Input validation: parse + validate the anomaly request before main processing.

    Rejects empty / oversized / non-JSON / structurally-invalid /
    unsupported-data_type input before the inner domain workflow graph runs.
    Deep field validation (identifier patterns, period format, finite-number
    checks on thresholds and metrics) happens in the inner ValidateInputNode -
    this node guarantees a well-formed envelope reaches it.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only
        if not isinstance(input_context, dict):
            input_context = {}

        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            emit_progress(EMPTY_INPUT)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        if len(user_input) > _MAX_INPUT_CHARS:
            # Never echo the payload; name the limit only.
            emit_progress(TOO_LONG)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [
                    f"PreProcessNode: user_input exceeds the maximum request size ({_MAX_INPUT_CHARS} characters)"
                ],
            }

        # Parse the JSON anomaly-detection request.
        try:
            payload = json.loads(user_input)
        except (json.JSONDecodeError, ValueError):
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    "PreProcessNode: user_input is not valid JSON (expected an anomaly-detection request object)"
                ],
            }

        if not isinstance(payload, dict):
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"PreProcessNode: request payload must be a JSON object, got {type(payload).__name__}"],
            }

        # Required fields: student_id + data_type.
        student_id = payload.get("student_id")
        data_type = payload.get("data_type")
        if not student_id or not isinstance(student_id, str):
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: missing or invalid 'student_id'"],
            }
        if not data_type or not isinstance(data_type, str):
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["PreProcessNode: missing or invalid 'data_type'"],
            }
        if data_type not in _ALLOWED_DATA_TYPES:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    f"PreProcessNode: unsupported data_type '{data_type}' (allowed: {sorted(_ALLOWED_DATA_TYPES)})"
                ],
            }

        # Normalise the period block.
        period = payload.get("period", {})
        if not isinstance(period, dict):
            period = {}
        normalised_period = {
            "start": str(period.get("start", "")),
            "end": str(period.get("end", "")),
        }

        # Carry optional threshold overrides + measured metrics through - both
        # are deep-validated (finite/bounded, fail-closed) in ValidateInputNode.
        thresholds = payload.get("thresholds", {})
        if not isinstance(thresholds, dict):
            thresholds = {}
        metrics = payload.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}

        anomaly_request: Dict[str, Any] = {
            "student_id": str(student_id),
            "data_type": data_type,
            "period": normalised_period,
            "thresholds": thresholds,
            "metrics": metrics,
        }

        # validated_input is a surface-PII-stripped echo of the raw request used
        # by the inner graph as its string entry point.
        validated_input = _surface_strip_pii(user_input.strip())

        # Telemetry channel tag: caller-controlled, so it is locked to an inert
        # identifier - any other shape degrades to the fixed "unknown" tag
        # (never the raw caller value).
        channel_raw = input_context.get("channel", "")
        channel = channel_raw if isinstance(channel_raw, str) and _INERT_IDENTIFIER_RE.match(channel_raw) else "unknown"

        # Domain audit: an anomaly-detection request was accepted + validated.
        emit_trace_event(
            "pre_process_validated",
            {"data_type": data_type, "input_chars": len(validated_input)},
            state,
        )

        return {
            "validated_input": validated_input,
            "anomaly_request": to_json(anomaly_request),
            "enriched_context": {
                "source": "StudentPerformanceAnomalyDetectionAgent",
                "channel": channel,
            },
            "status": AgentStatus.SUCCESS.value,
        }
