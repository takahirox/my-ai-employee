"""Offline, explicitly adjudicated false-rejection metrics; never acceptance authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from .domain.base import Digest, Identifier


class ContractObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    run_id: Identifier
    candidate_digest: Digest
    accepted: bool
    stable_code: str | None
    # These are operator/independent-test annotations, not claims inferred from a worker.
    independent_candidate_digest: Digest | None
    independent_evidence_digest: Digest | None
    substantive_pass: bool | None
    confirmed_contract_defect: bool | None
    legitimate_rejection: bool | None

    @model_validator(mode="after")
    def _exact_evidence(self) -> Self:
        if self.substantive_pass is not None and (
            self.independent_candidate_digest != self.candidate_digest
            or self.independent_evidence_digest is None
        ):
            raise ValueError("independent verdict requires evidence for this exact candidate")
        if self.confirmed_contract_defect and self.legitimate_rejection:
            raise ValueError("a legitimate rejection cannot be adjudicated as a contract defect")
        return self


def summarize_contract_observations(
    observations: tuple[ContractObservation, ...],
) -> dict[str, object]:
    identities = {(item.run_id, item.candidate_digest) for item in observations}
    if len(identities) != len(observations):
        raise ValueError("duplicate run/candidate observations would bias the metric")
    adjudicated = tuple(
        item
        for item in observations
        if item.substantive_pass is not None
        and item.confirmed_contract_defect is not None
        and item.legitimate_rejection is not None
    )
    false_rejections = sum(
        not item.accepted
        and item.substantive_pass is True
        and item.confirmed_contract_defect is True
        and not item.legitimate_rejection
        for item in adjudicated
    )
    return {
        "observed_candidates": len(observations),
        "adjudicated_candidates": len(adjudicated),
        "unadjudicated_candidates": len(observations) - len(adjudicated),
        "false_rejections": false_rejections,
        "false_rejection_rate": false_rejections / len(adjudicated) if adjudicated else None,
        "note": "Explicit offline adjudication; missing evidence is unknown, not zero failures.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    args = parser.parse_args()
    if args.observations.stat().st_size > 1_000_000:
        raise ValueError("contract audit input exceeds 1 MB")
    values = json.loads(args.observations.read_text())
    if not isinstance(values, list):
        raise ValueError("contract audit expects a JSON array of observations")
    observations = tuple(ContractObservation.model_validate(item, strict=True) for item in values)
    print(json.dumps(summarize_contract_observations(observations), sort_keys=True))


if __name__ == "__main__":
    main()
