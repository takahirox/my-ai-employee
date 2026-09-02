"""Read-only reporting for canonical productivity result bundles."""

from __future__ import annotations

import argparse
from pathlib import Path

from .productivity_evaluation import (
    ArmAggregate,
    MetricAggregate,
    PairedComparison,
    ResultBundle,
    aggregate_trials,
    compare_direct_to_fleet,
    load_result_bundle,
)
from .serialization import canonical_json

_METRIC_FAMILIES = {
    "quality": ("accepted", "authoritative_success", "regression_free"),
    "human_effort": ("human_active_seconds", "human_interventions"),
    "wall_time": ("time_to_accepted_seconds", "wall_seconds"),
    "cost_tokens": (
        "input_tokens",
        "output_tokens",
        "api_cost",
        "compute_seconds",
        "compute_cost",
        "context_input_tokens",
        "context_output_tokens",
    ),
    "reliability_recovery": (
        "retries",
        "repairs",
        "replans",
        "escalations",
        "recoveries",
    ),
    "orchestration": (
        "decomposed_nodes",
        "dependency_edges",
        "maximum_parallelism",
        "critical_path_seconds",
        "unnecessary_work_items",
        "unnecessary_work_seconds",
    ),
}
_FAMILY_TITLES = {
    "quality": "Quality and authoritative acceptance",
    "human_effort": "Human active time (operator attention)",
    "wall_time": "Wall-clock time (elapsed latency)",
    "cost_tokens": "Cost and tokens",
    "reliability_recovery": "Reliability and recovery",
    "orchestration": "Orchestration",
}


def _metric_families(
    metrics: tuple[MetricAggregate, ...],
) -> dict[str, tuple[MetricAggregate, ...]]:
    indexed = {item.metric: item for item in metrics}
    families = {
        family: tuple(indexed.pop(name) for name in names)
        for family, names in _METRIC_FAMILIES.items()
    }
    if indexed:
        raise ValueError(f"unclassified productivity metrics: {', '.join(sorted(indexed))}")
    return families


def _comparison(
    bundle: ResultBundle, direct_arm_id: str | None, fleet_arm_id: str | None
) -> PairedComparison | None:
    if (direct_arm_id is None) != (fleet_arm_id is None):
        raise ValueError("--direct-arm and --fleet-arm must be supplied together")
    if direct_arm_id is None or fleet_arm_id is None:
        return None
    direct = tuple(item for item in bundle.results if item.arm.id == direct_arm_id)
    fleet = tuple(item for item in bundle.results if item.arm.id == fleet_arm_id)
    if not direct:
        raise ValueError(f"direct arm not found: {direct_arm_id}")
    if not fleet:
        raise ValueError(f"Fleet arm not found: {fleet_arm_id}")
    return compare_direct_to_fleet(direct, fleet)


def _report_payload(
    bundle: ResultBundle,
    aggregates: tuple[ArmAggregate, ...],
    comparison: PairedComparison | None,
) -> dict[str, object]:
    paired: dict[str, object] | None = None
    if comparison is not None:
        paired = {
            "direct_arm_id": comparison.direct_arm_id,
            "fleet_arm_id": comparison.fleet_arm_id,
            "pairs": comparison.pairs,
            "metric_families": _metric_families(comparison.fleet_minus_direct),
            "task_classes_where_fleet_hurts": comparison.task_classes_where_fleet_hurts,
        }
    return {
        "schema_version": "2",
        "kind": "productivity_report",
        "bundle": {
            "id": bundle.id,
            "digest": bundle.bundle_digest,
            "benchmark": bundle.benchmark,
            "benchmark_version": bundle.benchmark_version,
            "trials": len(bundle.results),
        },
        "arms": tuple(
            {
                "arm_id": aggregate.arm_id,
                "trials": aggregate.trials,
                "metric_families": _metric_families(aggregate.metrics),
            }
            for aggregate in aggregates
        ),
        "paired_comparison": paired,
    }


def _number(value: float | None) -> str:
    return "—" if value is None else canonical_json(value)


def _append_metrics(lines: list[str], metrics: tuple[MetricAggregate, ...]) -> None:
    lines.extend(
        (
            "| Metric | Count | Mean | Rate | Sample variance |",
            "| --- | ---: | ---: | ---: | ---: |",
        )
    )
    for item in metrics:
        statistics = item.statistics
        lines.append(
            f"| `{item.metric}` | {statistics.count} | {_number(statistics.mean)} | "
            f"{_number(statistics.rate)} | {_number(statistics.sample_variance)} |"
        )


def _markdown_report(
    bundle: ResultBundle,
    aggregates: tuple[ArmAggregate, ...],
    comparison: PairedComparison | None,
) -> str:
    lines = [
        "# Fleet productivity report",
        "",
        f"- Bundle: `{bundle.id}`",
        f"- Digest: `{bundle.bundle_digest}`",
        f"- Benchmark: `{bundle.benchmark}` / `{bundle.benchmark_version}`",
        f"- Trials: {len(bundle.results)}",
        "",
        "> Human active time is operator attention. Wall-clock time is elapsed latency; "
        "the two are not interchangeable.",
    ]
    for family, title in _FAMILY_TITLES.items():
        lines.extend(("", f"## {title}"))
        for aggregate in aggregates:
            lines.extend(("", f"### Arm `{aggregate.arm_id}` ({aggregate.trials} trials)", ""))
            _append_metrics(lines, _metric_families(aggregate.metrics)[family])
    lines.extend(("", "## Paired direct vs Fleet"))
    if comparison is None:
        lines.extend(("", "No paired comparison requested."))
    else:
        lines.extend(
            (
                "",
                f"Direct `{comparison.direct_arm_id}` versus Fleet `{comparison.fleet_arm_id}`; "
                f"{comparison.pairs} paired trials. Deltas below are Fleet minus direct.",
            )
        )
        for family, title in _FAMILY_TITLES.items():
            lines.extend(("", f"### {title}", ""))
            _append_metrics(lines, _metric_families(comparison.fleet_minus_direct)[family])
        lines.extend(("", "### Task classes where Fleet hurts", ""))
        if comparison.task_classes_where_fleet_hurts:
            lines.extend(f"- `{item.value}`" for item in comparison.task_classes_where_fleet_hurts)
        else:
            lines.append("None observed in these paired trials.")
    return "\n".join(lines) + "\n"


def run_productivity(args: argparse.Namespace) -> int:
    """Validate or report a local bundle without workers, state, or network access."""

    bundle = load_result_bundle(Path(args.bundle).read_bytes())
    if args.productivity_command == "validate":
        print(
            canonical_json(
                {
                    "schema_version": "2",
                    "kind": "productivity_bundle_validation",
                    "valid": True,
                    "bundle_id": bundle.id,
                    "bundle_digest": bundle.bundle_digest,
                    "trials": len(bundle.results),
                }
            )
        )
        return 0
    if args.productivity_command != "report":
        raise ValueError(f"unsupported productivity command: {args.productivity_command}")
    if args.output_format not in {"json", "markdown"}:
        raise ValueError(f"unsupported output format: {args.output_format}")
    comparison = _comparison(bundle, args.direct_arm, args.fleet_arm)
    aggregates = aggregate_trials(bundle.results)
    if args.output_format == "markdown":
        print(_markdown_report(bundle, aggregates, comparison), end="")
    else:
        print(canonical_json(_report_payload(bundle, aggregates, comparison)))
    return 0
