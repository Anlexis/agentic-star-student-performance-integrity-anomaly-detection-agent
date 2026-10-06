"""AgentCore Platform v1.0"""

# EDU-C2-010 - CohortBaselineService
# Service layer: loads cohort baseline statistics for a given data_type +
# period.
#
# Must NOT contain business logic, routing, or credentials.
#
# This is a deterministic offline stub for the template: it synthesises stable
# baseline statistics keyed by data_type so the pipeline is fully testable
# offline. A production implementation would replace fetch_baseline() with a
# real query against the cohort warehouse / LMS analytics API, obtaining its
# connection credentials through the platform secrets contract per call -
# never storing them on this class or in agent State.

from __future__ import annotations

from typing import Any, Dict, Optional

# Deterministic per-data_type baseline metrics (offline stub).
# Each metric: mean / std / p25 / p75 / n_students.
_MOCK_BASELINES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "grades": {
        "grade_delta": {"mean": 0.0, "std": 8.0, "p25": -5.0, "p75": 5.0, "n_students": 240},
    },
    "attendance": {
        "attendance_rate": {"mean": 0.92, "std": 0.06, "p25": 0.88, "p75": 0.97, "n_students": 240},
    },
    "submission": {
        "submission_timing_offset": {"mean": -6.0, "std": 12.0, "p25": -14.0, "p75": 2.0, "n_students": 240},
    },
    "similarity": {
        "similarity_score": {"mean": 0.18, "std": 0.07, "p25": 0.12, "p75": 0.23, "n_students": 240},
    },
}


class CohortBaselineService:
    """Loads cohort baseline statistics for the EDU anomaly pipeline."""

    def fetch_baseline(
        self,
        data_type: str,
        period: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Return the cohort baseline for ``data_type`` (offline stub).

        Args:
            data_type: one of grades / attendance / submission / similarity.
            period: optional {"start", "end"} window (ignored by the stub;
                a real source would scope the cohort query to the period).

        Returns:
            {metric: {mean, std, p25, p75, n_students}} - empty dict if the
            data_type has no baseline available.
        """
        # period is accepted to mirror the real-source contract but the
        # offline stub does not use it.
        _ = period
        return dict(_MOCK_BASELINES.get(data_type, {}))
