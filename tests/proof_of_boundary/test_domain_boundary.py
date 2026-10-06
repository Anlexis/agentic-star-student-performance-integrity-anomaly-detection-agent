# PB - Domain / State-Serialization Boundary (PB-2 + PB-5 runtime complement)
#
# The AST-level state-safety scan lives in test_state_safety.py. This file adds
# the RUNTIME complement: after a full agent invocation, the surfaced state must
# contain only JSON-serialisable primitives (no Pydantic / dataclass / objects /
# JWT) - the checkpoint msgpack-safety guarantee - and the inner->outer key
# coupling (the domain boundary between DomainWorkflowGraph.get_output() and
# DomainWorkflowGraphNode.merge_output()) must hold.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import json


from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext

from src.graph.graph import Graph, DomainWorkflowGraphNode
from src.schemas.state import from_json, to_json


_PAYLOAD = json.dumps(
    {"student_id": "STU-2048", "data_type": "grades", "period": {"start": "2025-04", "end": "2025-09"}},
    ensure_ascii=False,
)

_PRIMITIVE_TYPES = (str, int, float, bool, type(None), list, dict)

# Credential / object markers that must never appear in the surfaced state.
_FORBIDDEN_MARKERS = ("eyJ", "sk-", "BEGIN PRIVATE KEY", "object at 0x")


def _run() -> dict:
    ctx = InvocationContext.for_internal(caller_id="test-suite")
    return Graph().invoke(_PAYLOAD, ctx=ctx)


def _assert_json_safe(value, path="<root>"):
    """Recursively assert a value is composed only of JSON primitives."""
    assert isinstance(value, _PRIMITIVE_TYPES), f"non-primitive at {path}: {type(value).__name__}"
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str), f"non-string key at {path}: {k!r}"
            _assert_json_safe(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_json_safe(v, f"{path}[{i}]")


class TestPostInvokeStateIsPrimitivesOnly:
    def test_run_succeeds(self):
        result = _run()
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')}. result={result!r}"

    def test_output_state_is_json_serialisable(self):
        result = _run()
        # The whole surfaced output must round-trip through JSON unharmed.
        dumped = json.dumps(result)
        assert json.loads(dumped) == result

    def test_output_state_values_are_primitives(self):
        result = _run()
        _assert_json_safe(result)

    def test_no_credential_or_object_markers_in_state(self):
        blob = json.dumps(_run(), ensure_ascii=False)
        for marker in _FORBIDDEN_MARKERS:
            assert marker not in blob, f"forbidden marker {marker!r} present in surfaced state"


class TestDomainKeyCoupling:
    """The inner get_output() emits keys the outer merge_output() reads back -
    the contract the two layers were designed against."""

    def test_merge_output_reads_inner_emitted_keys(self):
        # Simulate the inner sub_result exactly as DomainWorkflowGraph.get_output()
        # shapes it, then confirm the outer node maps the coupled keys through.
        inner_emitted = {
            "result": to_json({"anomaly_type": "grade_drop"}),
            "intervention_alert": to_json({"anomaly_type": "grade_drop"}),
            "anomaly_type": "grade_drop",
            "anomaly_confidence": 0.9,
            "status": AgentStatus.SUCCESS.value,
        }
        delta = DomainWorkflowGraphNode().merge_output({}, inner_emitted)
        assert delta["result"] == inner_emitted["result"]
        assert delta["intervention_alert"] == inner_emitted["intervention_alert"]
        assert delta["status"] == AgentStatus.SUCCESS.value

    def test_structured_state_fields_are_json_strings_not_containers(self):
        """Structured State fields are stored as JSON STRINGS. After a
        run, the inner alert surfaced under output is a string that itself parses
        back to a dict (never a bare dict in the checkpointed field)."""
        output = _run().get("output")
        assert isinstance(output, str)
        parsed = from_json(output)
        assert isinstance(parsed, dict)
