# EDU-C2-010 - Unit Tests: LoadCohortBaselineNode (inner domain node 2)
#
# Loads cohort baseline statistics for the requested data_type via
# CohortBaselineService (deterministic offline stub). A missing baseline
# degrades gracefully (empty dict + a node_history warning), never a hard
# fail. Credential material must never appear in the returned state delta -
# a production data source obtains its credentials through the platform
# secrets contract, never through State.
#
# Deterministic - no LLM, no network. framework.* / src.* imports only.

import inspect
import json

from src.nodes.load_cohort_baseline_node import LoadCohortBaselineNode
from src.schemas.state import from_json, to_json


def _state(data_type: str, **req_extra):
    req = {"student_id": "STU-1", "data_type": data_type, "period": {"start": "", "end": ""}}
    req.update(req_extra)
    return {"anomaly_request": to_json(req)}


class TestLoadCohortBaselineSuccess:
    def test_baseline_loaded_for_known_period(self):
        node = LoadCohortBaselineNode()
        result = node.execute(_state("grades"))
        baseline = from_json(result["cohort_baseline"])
        assert isinstance(baseline, dict)
        # grades baseline ships the grade_delta metric.
        assert "grade_delta" in baseline
        stats = baseline["grade_delta"]
        for key in ("mean", "std", "p25", "p75", "n_students"):
            assert key in stats

    def test_each_supported_data_type_has_a_baseline(self):
        node = LoadCohortBaselineNode()
        expected_metric = {
            "grades": "grade_delta",
            "attendance": "attendance_rate",
            "submission": "submission_timing_offset",
            "similarity": "similarity_score",
        }
        for data_type, metric in expected_metric.items():
            baseline = from_json(node.execute(_state(data_type))["cohort_baseline"])
            assert metric in baseline, f"{data_type} baseline missing {metric}"

    def test_cohort_baseline_is_json_string(self):
        node = LoadCohortBaselineNode()
        result = node.execute(_state("attendance"))
        assert isinstance(result["cohort_baseline"], str)


class TestLoadCohortBaselineDegradation:
    def test_missing_baseline_returns_empty_with_warning(self):
        """An unknown data_type yields an empty baseline + a node_history warning."""
        node = LoadCohortBaselineNode()
        result = node.execute(_state("unknown_type"))
        baseline = from_json(result["cohort_baseline"])
        assert baseline == {}
        assert "node_history" in result
        assert any("no cohort baseline" in n for n in result["node_history"])

    def test_missing_request_is_non_fatal(self):
        """from_json(None) -> {} default; node still returns a well-formed dict."""
        node = LoadCohortBaselineNode()
        result = node.execute({})
        assert isinstance(from_json(result["cohort_baseline"]), dict)


class TestLoadCohortBaselineCredentialSafety:
    def test_no_credential_material_in_state_delta(self):
        """The state delta carries baseline statistics only - never credential
        material, and never keys beyond the documented output contract."""
        node = LoadCohortBaselineNode()
        result = node.execute(_state("grades"))
        # The node may only return cohort_baseline (+ optional node_history).
        assert set(result.keys()) <= {"cohort_baseline", "node_history"}
        blob = json.dumps(result, ensure_ascii=False)
        assert "token" not in blob
        assert "credential" not in blob.lower() or "no cohort baseline" in blob

    def test_service_contract_takes_no_credentials(self):
        """The offline stub's fetch contract carries no credential parameter -
        a production source authenticates through the platform secrets
        contract instead of per-call state plumbing."""
        from src.services.cohort_baseline_service import CohortBaselineService

        params = inspect.signature(CohortBaselineService.fetch_baseline).parameters
        assert "credentials" not in params


class TestLoadCohortBaselineContract:
    def test_execute_method_signature(self):
        """Node contract: execute(self, state); no legacy invoke entry point."""
        assert hasattr(LoadCohortBaselineNode, "execute")
        params = list(inspect.signature(LoadCohortBaselineNode.execute).parameters.keys())
        assert len(params) >= 2
        assert params[1] == "state"
        assert "_invoke_impl" not in LoadCohortBaselineNode.__dict__
