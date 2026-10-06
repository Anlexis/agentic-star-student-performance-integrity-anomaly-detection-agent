# PB - Output Boundary: the gate blocks injected credentials in the alert
#
# EDU-C2-010's confidentiality guarantee for the returned intervention alert is
# enforced at the outer post_process slot (PostProcessNode._security_gate_output,
# a module-level helper invoked inside execute()) and additionally pre-screened by
# the terminal GenerateInterventionAlertNode. Both layers are asserted here:
#
#  (1) PostProcessNode credential gate (TestCredentialGate):
#      an injected secret in state["result"] (API key / JWT / Bearer / raw
#      credential assignment / student e-mail) yields ERROR and a sanitised stub
#      for BOTH formatted_output and result - the raw secret never survives.
#
#  (2) GenerateInterventionAlertNode pre-screen (TestGenerateAlertPrescreen):
#      a credential-bearing data_type echoed into the alert is blocked at the
#      terminal node with ERROR and NO alert/result emitted.
#
# PB-1 (emit_trace_event fires) and PB-6 (invoke execution order) are also
# asserted here against the node output-gate path.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import json
from unittest.mock import patch


from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext

from src.graph.graph import Graph
from src.nodes.post_process_node import PostProcessNode
from src.nodes.generate_intervention_alert_node import GenerateInterventionAlertNode
from src.schemas.state import to_json


# Simulated credential patterns - NOT real credentials.
_SIMULATED_API_KEY = "sk-TESTKEY1234567890abcdefghijklmn"
_SIMULATED_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
_SIMULATED_BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
_SIMULATED_CRED_STR = "password=super_secret_password_abc123"
_SIMULATED_EMAIL = "student.taro@example.com"

# A clean intervention alert (no credential strings) - must pass the gate.
_CLEAN_ALERT = json.dumps(
    {
        "anomaly_type": "grade_drop",
        "confidence": 0.9,
        "severity": "HIGH",
        "recommended_action": "Schedule academic support meeting",
        "evidence_summary": ["grade_delta: value=40.0, z=5.0, percentile=0.99"],
        "data_type": "grades",
    },
    ensure_ascii=False,
)


def _result_with(secret: str) -> str:
    return json.dumps(
        {
            "anomaly_type": "grade_drop",
            "severity": "HIGH",
            "debug_note": f"trace {secret}",
        },
        ensure_ascii=False,
    )


class TestCredentialGate:
    """PB (MANDATORY): PostProcessNode blocks credential-like output content.

    Drives PostProcessNode directly (the outer post_process slot is the output
    gate point). An injected secret in state["result"] must yield ERROR status and a
    sanitised stub for BOTH formatted_output and result.
    """

    def test_api_key_in_result_is_blocked(self):
        result = PostProcessNode().execute({"result": _result_with(_SIMULATED_API_KEY)})
        assert (
            result.get("status") == AgentStatus.ERROR.value
        ), f"Expected ERROR (gate block), got {result.get('status')}"

    def test_api_key_not_in_output_after_block(self):
        result = PostProcessNode().execute({"result": _result_with(_SIMULATED_API_KEY)})
        assert _SIMULATED_API_KEY not in result.get("formatted_output", "")
        assert _SIMULATED_API_KEY not in result.get("result", "")

    def test_blocked_output_is_sanitised_stub(self):
        result = PostProcessNode().execute({"result": _result_with(_SIMULATED_API_KEY)})
        for field in ("formatted_output", "result"):
            output = result.get(field, "")
            assert (
                "BLOCKED" in output or "blocked" in output.lower()
            ), f"Expected sanitised stub in blocked {field}, got: {output[:200]!r}"
        assert result.get("error_log"), "Blocked output must carry an error_log entry"

    def test_jwt_in_result_is_blocked(self):
        result = PostProcessNode().execute({"result": _result_with(_SIMULATED_JWT)})
        assert result.get("status") == AgentStatus.ERROR.value

    def test_bearer_token_in_result_is_blocked(self):
        result = PostProcessNode().execute({"result": json.dumps({"auth": _SIMULATED_BEARER}, ensure_ascii=False)})
        assert result.get("status") == AgentStatus.ERROR.value

    def test_credential_assignment_in_result_is_blocked(self):
        result = PostProcessNode().execute({"result": json.dumps({"cfg": _SIMULATED_CRED_STR}, ensure_ascii=False)})
        assert result.get("status") == AgentStatus.ERROR.value
        assert _SIMULATED_CRED_STR not in result.get("formatted_output", "")
        assert _SIMULATED_CRED_STR not in result.get("result", "")

    def test_student_email_in_result_is_blocked(self):
        """Student PII (e-mail) must never surface in a returned alert."""
        result = PostProcessNode().execute({"result": json.dumps({"contact": _SIMULATED_EMAIL}, ensure_ascii=False)})
        assert result.get("status") == AgentStatus.ERROR.value
        assert _SIMULATED_EMAIL not in result.get("formatted_output", "")

    def test_clean_alert_passes_the_gate(self):
        """A clean intervention alert must NOT trip the gate (SUCCESS, unchanged)."""
        result = PostProcessNode().execute({"result": _CLEAN_ALERT})
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Clean alert should pass the output gate, got {result.get('status')}. "
            f"error_log: {result.get('error_log')}"
        )
        assert result.get("formatted_output") == _CLEAN_ALERT


class TestGenerateAlertPrescreen:
    """PB: the terminal node refuses to emit an alert carrying a secret."""

    def _state(self, data_type: str):
        return {
            "anomaly_type": "grade_drop",
            "anomaly_confidence": 0.9,
            "deviation_scores": to_json(
                {"grade_delta": {"z_score": 5.0, "percentile": 0.99, "is_anomalous": True, "source": "measured"}}
            ),
            "anomaly_request": to_json({"student_id": "STU-1", "data_type": data_type}),
        }

    def test_credential_in_alert_is_blocked_and_not_emitted(self):
        node = GenerateInterventionAlertNode()
        # The data_type echoes into the alert; inject an API key there.
        result = node.execute(self._state(f"grades {_SIMULATED_API_KEY}"))
        assert result.get("status") == AgentStatus.ERROR.value
        # On block the node emits NO intervention_alert / result.
        assert "intervention_alert" not in result
        assert "result" not in result
        assert result.get("error_log")

    def test_clean_data_type_emits_alert(self):
        node = GenerateInterventionAlertNode()
        result = node.execute(self._state("grades"))
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert _SIMULATED_API_KEY not in result.get("result", "")


class TestPB1EmitTraceEventFires:
    """PB-1: BaseNode -> EventEmitter boundary - emit_trace_event fires on the
    happy path of every domain/backbone node (audit trail, no silent failures)."""

    def test_pre_process_emits_event(self):
        with patch("src.nodes.pre_process_node.emit_trace_event") as spy:
            from src.nodes.pre_process_node import PreProcessNode

            PreProcessNode().execute({"user_input": json.dumps({"student_id": "STU-1", "data_type": "grades"})})
            assert spy.called, "PreProcessNode must emit a trace event"

    def test_post_process_emits_event_on_clean_output(self):
        with patch("src.nodes.post_process_node.emit_trace_event") as spy:
            PostProcessNode().execute({"result": _CLEAN_ALERT})
            assert spy.called, "PostProcessNode must emit a trace event on clean output"

    def test_generate_alert_emits_event(self):
        with patch("src.nodes.generate_intervention_alert_node.emit_trace_event") as spy:
            GenerateInterventionAlertNode().execute(
                {
                    "anomaly_type": "grade_drop",
                    "anomaly_confidence": 0.9,
                    "deviation_scores": to_json({}),
                    "anomaly_request": to_json({"student_id": "STU-1", "data_type": "grades"}),
                }
            )
            assert spy.called, "GenerateInterventionAlertNode must emit a trace event"

    def test_classify_anomaly_emits_event(self):
        with patch("src.nodes.classify_anomaly_node.emit_trace_event") as spy:
            from src.nodes.classify_anomaly_node import ClassifyAnomalyNode

            ClassifyAnomalyNode().execute({"deviation_scores": to_json({})})
            assert spy.called, "ClassifyAnomalyNode must emit a trace event"


class TestPB6InvokeExecutionOrder:
    """PB-6: invoke execution order - the trust gate runs (and DENIES) before
    execute() when the caller trust level is insufficient.

    The backbone reads caller_trust_level from state (written by invoke()). An
    ANONYMOUS caller must be denied at PreProcessNode (required VERIFIED_EXTERNAL)
    BEFORE any business logic runs - proving the gate runs before execute().
    """

    _PAYLOAD = json.dumps({"student_id": "STU-1", "data_type": "grades"}, ensure_ascii=False)

    def test_anonymous_caller_is_denied_before_execute(self):
        # Default InvocationContext = ANONYMOUS trust.
        result = Graph().invoke(self._PAYLOAD, ctx=InvocationContext())
        assert result.get("status") != AgentStatus.SUCCESS.value, "ANONYMOUS caller must not reach a successful result"

    def test_internal_caller_passes_the_gate(self):
        result = Graph().invoke(self._PAYLOAD, ctx=InvocationContext.for_internal(caller_id="t"))
        assert result.get("status") == AgentStatus.SUCCESS.value
