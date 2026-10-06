"""AgentCore Platform v1.0"""

# EDU-C2-010 - DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full student-performance-anomaly domain workflow:
#
#   START -> validate_input -> load_cohort_baseline -> detect_deviations
#         -> classify_anomaly -> generate_intervention_alert -> END
#
# Called by DomainWorkflowGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   [x] Inherits BaseGraph (fully custom topology - no forced backbone)
#   [x] Implements all 7 BaseGraph ABC methods
#   [x] register_nodes() does NOT call super() (abstract in BaseGraph)
#   [x] register_nodes() instantiates every domain node with NO ctor args
#   [x] Does NOT register initialize / finalize (outer backbone concerns)
#   [x] get_output() designed together with DomainWorkflowGraphNode.merge_output()
#   [x] No platform-internal SDK imports
#   [x] Not placed under src/subagents/

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.classify_anomaly_node import ClassifyAnomalyNode
from src.nodes.detect_deviations_node import DetectDeviationsNode
from src.nodes.generate_intervention_alert_node import GenerateInterventionAlertNode
from src.nodes.load_cohort_baseline_node import LoadCohortBaselineNode
from src.nodes.validate_input_node import ValidateInputNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for EDU-C2-010.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by DomainWorkflowGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          -> validate_input             (ValidateInputNode)            - caller-data contract (fail-closed)
          -> load_cohort_baseline       (LoadCohortBaselineNode)       - load cohort statistics
          -> detect_deviations          (DetectDeviationsNode)         - per-metric z-scores
          -> classify_anomaly           (ClassifyAnomalyNode)          - anomaly_type + confidence
          -> generate_intervention_alert(GenerateInterventionAlertNode)- alert + result
          -> END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns - not registered here.
    """

    # -- Identity --

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "edu_c2_010_anomaly_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # -- Config validation --

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        The anomaly_thresholds block is forwarded by
        DomainWorkflowGraphNode._parent_config() and shape-guarded again in
        _extra_initial_state(); absence is non-fatal (nodes fall back to
        documented defaults), so this validation is permissive rather than
        raising ConfigError.
        """

    # -- Initial-state seeding --

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the runtime config and the caller context.

        Two hand-offs happen here, both invisible to the outer backbone:

        1. `thresholds_config`: DomainWorkflowGraphNode._parent_config()
           forwards the config/config.yaml `anomaly_thresholds` block under
           config["configurable"]; this hook republishes it as the JSON-string
           state field `thresholds_config` (structured State fields travel as
           JSON strings for checkpoint msgpack safety). ValidateInputNode /
           GenerateInterventionAlertNode read this state field.

        2. `input_context`: GraphNode.execute() does not forward input_context
           on subgraph.invoke() (SDK 1.0.1);
           DomainWorkflowGraphNode.extract_input() stashes it via the context
           bridge immediately before the inner invoke, and this hook reads it
           back - see src/graph/context_bridge.py. ValidateInputNode enforces
           the caller-data contract on the bridged values (fail-closed).
        """
        configurable = (self.config or {}).get("configurable", {})
        thresholds = configurable.get("anomaly_thresholds")
        if not isinstance(thresholds, dict):
            thresholds = {}
        return {
            "thresholds_config": to_json(thresholds),
            "input_context": get_caller_input_context(),
        }

    # -- Node registration --

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call - BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.

        Every node is instantiated with NO constructor arguments - tuning
        values reach the nodes via the state-seeded `thresholds_config` field
        (_extra_initial_state() above), never a constructor parameter. Every
        key registered here is referenced in add_edges().
        """
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["load_cohort_baseline"] = LoadCohortBaselineNode()
        self._nodes["detect_deviations"] = DetectDeviationsNode()
        self._nodes["classify_anomaly"] = ClassifyAnomalyNode()
        self._nodes["generate_intervention_alert"] = GenerateInterventionAlertNode()

    # -- Edge wiring --

    def add_edges(self) -> None:
        """Wire the linear anomaly-detection domain topology.

        Each step passes its partial-dict output into the shared State.
        For this template the topology is intentionally linear - no conditional
        branching between domain nodes. route() is implemented as required by
        the ABC but add_conditional_edges() is not used. A mid-pipeline ERROR
        does not need conditional routing: the framework skips execute() on
        every node whose incoming state already carries ERROR status.
        """
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "load_cohort_baseline")
        self._sg.add_edge("load_cohort_baseline", "detect_deviations")
        self._sg.add_edge("detect_deviations", "classify_anomaly")
        self._sg.add_edge("classify_anomaly", "generate_intervention_alert")
        self._sg.add_edge("generate_intervention_alert", END)

    # -- Routing --

    def route(self, state: AgentState) -> str:
        """Conditional routing - required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. It is implemented to satisfy the ABC
        contract. Returns END on error so an unexpected call does not re-enter a
        processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return str(END)
        return "generate_intervention_alert"

    # -- Output shape --

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by DomainWorkflowGraphNode.merge_output() in
        graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()  emits: "result", "intervention_alert", "status", ...
            Outer merge_output() reads: sub_result.get("result"),
                                        sub_result.get("intervention_alert"),
                                        sub_result.get("status")

        Additional fields (anomaly_type, anomaly_confidence, deviation_scores,
        trace_id, correlation_id, node_history) are surfaced for observability /
        downstream extension.
        """
        return {
            # the reason must leave the subgraph or the outer graph cannot report it
            "error_code": state.get("error_code"),
            "result": state.get("result"),
            "intervention_alert": state.get("intervention_alert"),
            "anomaly_type": state.get("anomaly_type"),
            "anomaly_confidence": state.get("anomaly_confidence"),
            "deviation_scores": state.get("deviation_scores"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
