# EDU-C2-010 - Unit Tests: main slot (DomainWorkflowGraphNode)
#
# The `main` backbone slot of StudentPerformanceAnomalyDetectionAgent is a
# GraphNode subclass (DomainWorkflowGraphNode) that delegates to the inner
# DomainWorkflowGraph. These tests exercise its real contracts in isolation,
# without driving the whole agent (the full end-to-end run lives in
# test_graph_composition.py):
#
#   - get_subgraph()  -> returns a DomainWorkflowGraph instance (no ctor args)
#   - extract_input() -> reads validated_input (falls back to user_input)
#   - merge_output()  -> maps inner sub_result onto the outer state delta
#                        (result / intervention_alert / anomaly_* / status),
#                        returning only changed keys
#
# The production main
# slot is the GraphNode wrapper, not a standalone FunctionNode.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.


from framework.schemas.agent_status import AgentStatus

from src.graph.graph import (
    DomainWorkflowGraphNode,
    StudentPerformanceAnomalyDetectionAgent,
    Graph,
)
from src.graph.domain_workflow_graph import DomainWorkflowGraph


class TestDomainWorkflowGraphNodeSubgraph:
    """get_subgraph() must return the inner DomainWorkflowGraph (no ctor args)."""

    def test_get_subgraph_returns_domain_workflow_graph(self):
        node = DomainWorkflowGraphNode()
        sub = node.get_subgraph()
        assert isinstance(
            sub, DomainWorkflowGraph
        ), f"get_subgraph() must return DomainWorkflowGraph, got {type(sub).__name__}"

    def test_get_subgraph_returns_fresh_instances(self):
        node = DomainWorkflowGraphNode()
        assert node.get_subgraph() is not node.get_subgraph()


class TestDomainWorkflowGraphNodeExtractInput:
    """extract_input() prefers validated_input, falls back to user_input."""

    def test_prefers_validated_input(self):
        node = DomainWorkflowGraphNode()
        state = {"validated_input": "PII-stripped payload", "user_input": "raw"}
        assert node.extract_input(state) == "PII-stripped payload"

    def test_falls_back_to_user_input(self):
        node = DomainWorkflowGraphNode()
        assert node.extract_input({"user_input": "raw request"}) == "raw request"

    def test_empty_when_neither_present(self):
        node = DomainWorkflowGraphNode()
        assert node.extract_input({}) == ""


class TestDomainWorkflowGraphNodeMergeOutput:
    """merge_output() maps inner sub_result -> outer delta (changed keys only)."""

    def test_maps_inner_result_to_outer_keys(self):
        node = DomainWorkflowGraphNode()
        sub_result = {
            "result": '{"anomaly_type": "grade_drop"}',
            "intervention_alert": '{"anomaly_type": "grade_drop"}',
            "anomaly_type": "grade_drop",
            "anomaly_confidence": 0.9,
            "status": AgentStatus.SUCCESS.value,
        }
        delta = node.merge_output({}, sub_result)
        assert delta["result"] == sub_result["result"]
        assert delta["intervention_alert"] == sub_result["intervention_alert"]
        assert delta["anomaly_type"] == "grade_drop"
        assert delta["anomaly_confidence"] == 0.9
        assert delta["status"] == AgentStatus.SUCCESS.value

    def test_returns_only_changed_keys(self):
        node = DomainWorkflowGraphNode()
        delta = node.merge_output(
            {"unrelated": "keep me out"},
            {"result": "r", "status": AgentStatus.SUCCESS.value},
        )
        assert set(delta.keys()) == {
            "result",
            "intervention_alert",
            "anomaly_type",
            "anomaly_confidence",
            "status",
            # carried across the boundary so the outer graph can report a
            # rejection settled before the inner run
            "error_code",
        }

    def test_missing_fields_yield_none(self):
        node = DomainWorkflowGraphNode()
        delta = node.merge_output({}, {})
        assert delta["result"] is None
        assert delta["intervention_alert"] is None
        assert delta["status"] is None


class TestOuterAgentIdentity:
    """Agent identity + Graph alias + main-slot wiring."""

    def test_name_property(self):
        assert StudentPerformanceAnomalyDetectionAgent().name == "StudentPerformanceAnomalyDetectionAgent"

    def test_graph_alias_is_outer_class(self):
        assert Graph is StudentPerformanceAnomalyDetectionAgent

    def test_main_slot_is_domain_workflow_graph_node(self):
        agent = StudentPerformanceAnomalyDetectionAgent()
        agent.register_nodes()
        assert isinstance(agent._nodes["main"], DomainWorkflowGraphNode)

    def test_error_strategy_is_propagate(self):
        # Cat 2 fail-fast: inner errors re-raise as SubgraphError.
        assert DomainWorkflowGraphNode.error_strategy == "propagate"
