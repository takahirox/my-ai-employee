from __future__ import annotations

import pytest

from ai_employee.contract_audit import ContractObservation, summarize_contract_observations
from ai_employee.domain import GoalTaskKind
from ai_employee.orchestration import WorkCoordinator
from tests.test_graph_typed_results import _execute


def observation(**changes):
    return ContractObservation(
        **{
            "run_id": "run",
            "candidate_digest": "0" * 64,
            "accepted": False,
            "stable_code": "VERIFICATION_BINDING_INVALID",
            "independent_candidate_digest": "0" * 64,
            "independent_evidence_digest": "1" * 64,
            "substantive_pass": True,
            "confirmed_contract_defect": True,
            "legitimate_rejection": False,
            **changes,
        }
    )


def test_false_rejection_metric_requires_adjudication_and_exact_independent_evidence():
    values = (
        observation(),
        observation(run_id="accepted", accepted=True, confirmed_contract_defect=False),
        observation(
            run_id="bad",
            substantive_pass=False,
            confirmed_contract_defect=False,
            legitimate_rejection=True,
        ),
        observation(
            run_id="unknown",
            substantive_pass=None,
            confirmed_contract_defect=None,
            legitimate_rejection=None,
            independent_candidate_digest=None,
            independent_evidence_digest=None,
        ),
    )
    summary = summarize_contract_observations(values)
    assert summary["false_rejections"] == 1 and summary["false_rejection_rate"] == 1 / 3
    assert summary["unadjudicated_candidates"] == 1
    assert summarize_contract_observations(())["false_rejection_rate"] is None
    with pytest.raises(ValueError, match="exact candidate"):
        observation(independent_candidate_digest="2" * 64)
    with pytest.raises(ValueError, match="legitimate"):
        observation(legitimate_rejection=True)
    with pytest.raises(ValueError, match="duplicate"):
        summarize_contract_observations((values[0], values[0]))


def test_missing_typed_artifact_budget_stops_before_worker_probe(tmp_path, monkeypatch):
    original = WorkCoordinator.start
    observed = []

    def omit_budget(self, *args, **kwargs):
        request = kwargs.get("_accepted_request")
        if request is not None and request.task_kind is GoalTaskKind.NON_MUTATING:
            kwargs["_accepted_request"] = request.model_copy(update={"remaining_budgets": {}})
            self.worker_factory = lambda *_: pytest.fail(
                "preflight must precede worker factory/probe"
            )
        run = original(self, *args, **kwargs)
        observed.append(run.failure_code)
        return run

    monkeypatch.setattr(WorkCoordinator, "start", omit_budget)
    _execute(tmp_path)
    assert observed and observed[0] == "ARTIFACT_BUDGET_INVALID"
