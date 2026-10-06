"""AgentCore Platform v1.0"""

# EDU-C2-010 - Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# StudentPerformanceAnomalyDetectionAgent (anomaly-detection pipeline, Cat 2).
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed - identical to Cat 1, do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                              (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (DomainWorkflowGraphNode) that
#   delegates the full student-performance-anomaly domain workflow to
#   DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                  outer graph (this file)
#   src/graph/domain_workflow_graph.py  inner graph (multi-step topology)
#   src/graph/context_bridge.py         input_context hand-off outer -> inner
#
# Rules enforced:
#   [x] StudentPerformanceAnomalyDetectionAgent inherits AgentBaseGraph
#       (framework base class - direct framework inheritance)
#   [x] super().register_nodes() called first (fills initialize + finalize)
#   [x] DomainWorkflowGraphNode assigned to self._nodes["main"]
#   [x] _parent_config() forwards the config/config.yaml anomaly_thresholds
#       block to the inner graph (never a dead read of the static manifest)
#   [x] extract_input() bridges the caller's input_context to the inner graph
#       (src/graph/context_bridge.py)
#   [x] merge_output() returns only changed keys
#   [x] add_edges() NOT overridden on the outer graph
#   [x] No platform-internal SDK imports

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from framework.schemas.agent_status import AgentStatus
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.graph.context_bridge import set_caller_input_context
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import DomainWorkflowGraph

# Runtime-config path: src/graph/graph.py -> parents[2] = repo root.
# config/config.yaml holds the runtime parameters (max_retry, timeout_s) and
# the anomaly_thresholds tuning block; config/agent.yaml is the static
# manifest (identity, entry point, trust level) and carries no tuning values.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Fallback mirrors the `anomaly_thresholds` block in config/config.yaml so
# _parent_config() never forwards an empty config even if the file is
# unreadable in an exotic deployment layout.
_FALLBACK_ANOMALY_THRESHOLDS: dict[str, Any] = {
    "z_score_threshold": 2.0,
    "confidence_high": 0.7,
    "confidence_medium": 0.4,
}


def _runtime_config() -> dict[str, Any]:
    """Read the runtime tuning blocks from config/config.yaml.

    Returns {} - never raises - when the file is absent, unreadable, not valid
    YAML, or not a mapping. Callers apply the module fallbacks in that case.
    The static manifest (config/agent.yaml) is deliberately NOT read here: it
    declares identity and compile-time requirements only, so a reader pointed
    at it would silently return no tuning values at all.
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


class DomainWorkflowGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of the outer graph.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    - instantiate DomainWorkflowGraph with the forwarded
                          runtime config (_parent_config())
      extract_input()   - pull validated_input (PII-stripped) from the outer
                          state and stash input_context for the inner graph
                          (context bridge)
      merge_output()    - map sub_result fields into outer state delta (changed
                          keys only)
      error_strategy    - "propagate": re-raise inner errors as SubgraphError
                          (fail-fast; the framework converts the raised error
                          into a terminal ERROR status on the outer state)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default - fail fast).
    # "handle": call on_subgraph_error() instead - use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: this template does not use human-in-the-loop interrupts at all.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> dict[str, Any]:
        """Forward the `anomaly_thresholds` tuning block to the inner graph.

        Loads config/config.yaml (the runtime parameters file) and returns the
        block under config["configurable"] - never an empty dict. The inner
        graph republishes it into inner state
        (DomainWorkflowGraph._extra_initial_state()) as the JSON-string field
        `thresholds_config`, so the domain nodes read live threshold values
        (z_score_threshold, confidence bands) instead of dead configuration
        text.
        """
        runtime = _runtime_config()
        thresholds = runtime.get("anomaly_thresholds")
        if not isinstance(thresholds, dict) or not thresholds:
            thresholds = dict(_FALLBACK_ANOMALY_THRESHOLDS)
        return {"configurable": {"anomaly_thresholds": thresholds}}

    def get_subgraph(self) -> "DomainWorkflowGraph":
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time.

        The inner graph receives the runtime config via its BaseGraph ctor
        (graph-level constructor injection of immutable config - distinct from
        the per-node execute() contract); its domain NODES still take no
        constructor arguments and read tuning values exclusively via the
        state-seeded `thresholds_config` field.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined by pre_process has no validated input to act on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates and PII-strips the raw user_input and writes
        the result to validated_input. Prefer that; fall back to user_input if
        validated_input is absent (e.g. in unit tests).

        Also bridges the caller's input_context to the inner graph:
        GraphNode.execute() does not forward input_context on subgraph.invoke()
        (SDK 1.0.1), and extract_input is the last template-code hook that sees
        the outer state before the inner invoke - see
        src/graph/context_bridge.py. The bridged values are UNVALIDATED here;
        the inner ValidateInputNode enforces the caller-data contract
        (fail-closed) before any downstream node reads them.
        """
        set_caller_input_context(state.get("input_context") or {})
        return cast(str, state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys - never the full state.

        GraphNode.execute() raises SubgraphError before calling merge_output
        when the inner graph terminates with ERROR status (error_strategy
        "propagate"), so this method only runs on a successful inner pass.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "result", "intervention_alert", "status", ...
          This merge_output() reads -> sub_result.get("result"),
                                      sub_result.get("intervention_alert"),
                                      sub_result.get("status")

        result (str | None): serialised intervention alert; written by
          GenerateInterventionAlertNode inside the inner graph. PostProcessNode
          (outer post_process slot) reads state.get("result") for the output
          gate.
        intervention_alert (str | None): the structured alert (JSON string).
        status (str | None): terminal AgentStatus value from the inner graph run.
        """
        return {
            # Outer reason wins: a reason settled before the inner run is the real
            # one, and a plain sub_result.get() would erase it.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
            "result": sub_result.get("result"),
            "intervention_alert": sub_result.get("intervention_alert"),
            "anomaly_type": sub_result.get("anomaly_type"),
            "anomaly_confidence": sub_result.get("anomaly_confidence"),
            "status": sub_result.get("status"),
        }


class StudentPerformanceAnomalyDetectionAgent(AgentBaseGraph):
    """Outer graph for EDU-C2-010 (Cat 2).

    Inherits AgentBaseGraph (framework base class) directly. Domain logic is
    fully encapsulated in DomainWorkflowGraphNode (main slot), which delegates
    to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed - identical to Cat 1):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (input validation + surface PII strip)
      - main:         DomainWorkflowGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (output security gate + schema enforcement)

    add_edges() is NOT overridden - backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "StudentPerformanceAnomalyDetectionAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first - it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = DomainWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.


# Back-compat alias - config/agent.yaml declares the class as
# "StudentPerformanceAnomalyDetectionAgent". `Graph` is kept for any tooling
# that references the generic name.
Graph = StudentPerformanceAnomalyDetectionAgent
