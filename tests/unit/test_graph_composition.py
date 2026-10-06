# EDU-C2-010 - Unit Tests: nested Cat-2 graph composition (end-to-end)
#
# Drives the REAL outer agent (StudentPerformanceAnomalyDetectionAgent / Graph)
# end-to-end via the AgentBaseGraph compile-on-first-use invoke() path - the
# same nested-composition pattern used across Cat 2 templates. The inner
# DomainWorkflowGraph runs all 5 domain nodes; GenerateInterventionAlertNode
# sets status=SUCCESS, which the outer merge_output maps to the outer state so
# the backbone routes main -> post_process -> finalize.
#
# Deterministic - the pipeline is rule-based statistics (no LLM, no network).
# DetectDeviationsNode derives a stable pseudo-measurement from the opaque
# student_id, so a given payload always produces the same verdict.
# framework.* / src.* imports only.

import json


from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext

from src.graph.graph import StudentPerformanceAnomalyDetectionAgent, Graph
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.schemas.state import State


_PAYLOAD = json.dumps(
    {
        "student_id": "STU-2048",
        "data_type": "grades",
        "period": {"start": "2025-04", "end": "2025-09"},
        "thresholds": {"z_score": 2.0},
    },
    ensure_ascii=False,
)


class TestOuterGraphConstruction:
    def test_state_schema_is_state(self):
        assert StudentPerformanceAnomalyDetectionAgent().state_schema is State

    def test_compile_fills_all_backbone_slots(self):
        agent = StudentPerformanceAnomalyDetectionAgent()
        agent.compile()
        for slot in ("initialize", "pre_process", "main", "post_process", "finalize"):
            assert agent._nodes.get(slot) is not None, f"backbone slot not filled: {slot}"


class TestInnerGraphConstruction:
    def test_inner_graph_registers_five_domain_nodes(self):
        inner = DomainWorkflowGraph()
        inner.register_nodes()
        assert set(inner._nodes.keys()) == {
            "validate_input",
            "load_cohort_baseline",
            "detect_deviations",
            "classify_anomaly",
            "generate_intervention_alert",
        }

    def test_inner_graph_name_and_schema(self):
        inner = DomainWorkflowGraph()
        assert inner.name == "edu_c2_010_anomaly_workflow"
        assert inner.state_schema is State

    def test_inner_graph_get_output_shape(self):
        inner = DomainWorkflowGraph()
        out = inner.get_output({"result": "R", "intervention_alert": "A", "status": AgentStatus.SUCCESS.value})
        assert out["result"] == "R"
        assert out["intervention_alert"] == "A"
        assert out["status"] == AgentStatus.SUCCESS.value


class TestEndToEndInvoke:
    """Full agent run: outer backbone + inner domain workflow, no LLM."""

    def _run(self, user_input: str) -> dict:
        agent = Graph()  # back-compat alias for StudentPerformanceAnomalyDetectionAgent
        # Secure-by-Default: the backbone trust gate denies ANONYMOUS callers
        # (PreProcessNode requires VERIFIED_EXTERNAL, the domain nodes INTERNAL).
        # Supply a fully-trusted internal context, exactly as AgentGateway would
        # for an authenticated internal caller.
        ctx = InvocationContext.for_internal(caller_id="test-suite")
        return agent.invoke(user_input, ctx=ctx)

    def test_invoke_returns_success(self):
        result = self._run(_PAYLOAD)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    def test_invoke_output_is_populated_alert(self):
        result = self._run(_PAYLOAD)
        # Outer get_output() surfaces formatted_output (mapped from the inner
        # result) under "output".
        output = result.get("output")
        assert isinstance(output, str) and output.strip(), f"Expected a non-empty alert in output, got {output!r}"

    def test_output_is_a_well_formed_intervention_alert(self):
        result = self._run(_PAYLOAD)
        alert = json.loads(result.get("output", "{}"))
        # The serialised intervention alert carries the documented shape.
        for key in ("anomaly_type", "confidence", "severity", "recommended_action", "evidence_summary"):
            assert key in alert, f"intervention alert missing {key}: {alert}"
        assert isinstance(alert["evidence_summary"], list)
        assert 0.0 <= float(alert["confidence"]) <= 1.0

    def test_node_history_records_backbone_traversal(self):
        result = self._run(_PAYLOAD)
        history = result.get("node_history", [])
        assert isinstance(history, list)
        # BaseNode.__call__ appends each node's CLASS name (not the slot name).
        # The outer backbone runs the input gate (PreProcessNode), the main slot
        # wrapper (DomainWorkflowGraphNode) and the output gate (PostProcessNode).
        for cls_name in ("PreProcessNode", "DomainWorkflowGraphNode", "PostProcessNode"):
            assert cls_name in history, f"node_history missing {cls_name}: {history}"

    def test_non_json_input_degrades_gracefully_without_crashing(self):
        """Non-JSON input is declined by PreProcessNode and the run COMPLETES
        carrying the reason: the caller reads what to correct and can resend on
        the same conversation, instead of the turn ending on an exception type.

        Nothing downstream runs on the declined input, so the body is the reason
        sentence rather than an alert document.

        Per-node input rejection is asserted directly in
        tests/proof_of_boundary/test_validation_boundary.py; here we only prove the
        full run never raises and always yields a terminal status + a body."""
        result = self._run("this is not a valid anomaly request")
        assert result.get("status") == AgentStatus.SUCCESS.value
        output = result.get("output")
        assert output, result
        # the reason sentence, not an alert payload
        assert "anomaly_type" not in output

    def test_deterministic_repeated_runs_match(self):
        """The same payload yields the same verdict across runs (no randomness)."""
        first = self._run(_PAYLOAD).get("output", "")
        second = self._run(_PAYLOAD).get("output", "")
        assert first == second
