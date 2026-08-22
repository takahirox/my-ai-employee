from __future__ import annotations

import math
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from ai_employee.domain import AcceptedGraphRevision, Event, FrozenDict, Goal
from ai_employee.serialization import (
    canonical_digest,
    canonical_json,
    dumps_yaml,
    loads_model,
    loads_yaml_model,
)
from tests.helpers import graph


class SerializationTests(unittest.TestCase):
    def test_unknown_fields_and_unsupported_schema_versions_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            Goal(id="goal.one", statement="x", unexpected=True)  # type: ignore[call-arg]
        with self.assertRaises(ValidationError):
            Goal(id="goal.one", statement="x", schema_version="2")

    def test_canonical_json_is_stable_and_rejects_non_finite_numbers(self) -> None:
        self.assertEqual(canonical_json({"z": 1, "a": 2}), '{"a":2,"z":1}')
        with self.assertRaises(ValueError):
            canonical_json({"unsafe": math.inf})

    def test_canonical_json_rejects_non_string_mapping_keys(self) -> None:
        with self.assertRaisesRegex(TypeError, "object keys must be strings"):
            canonical_json({1: "not canonical"})

    def test_typed_json_and_yaml_round_trip(self) -> None:
        original = Goal(id="goal.one", statement="Round trip")
        restored_json = loads_model(canonical_json(original), Goal)
        restored_yaml = loads_yaml_model(dumps_yaml(original), Goal)
        self.assertEqual(restored_json, original)
        self.assertEqual(restored_yaml, original)

    def test_yaml_cannot_coerce_non_string_mapping_keys(self) -> None:
        parsed = {
            "id": "event.one",
            "run_id": "run.one",
            "event_type": "test.event",
            "timestamp": "2025-01-01T00:00:00Z",
            "actor": "runtime",
            "payload": {1: "forbidden"},
        }
        yaml_stub = SimpleNamespace(safe_load=lambda _text: parsed)
        with (
            patch.dict(sys.modules, {"yaml": yaml_stub}),
            self.assertRaisesRegex(TypeError, "object keys must be strings"),
        ):
            loads_yaml_model("not JSON", Event)

    def test_accepted_revision_digest_is_stable_and_verified(self) -> None:
        first = AcceptedGraphRevision(revision_number=1, graph=graph())
        second = AcceptedGraphRevision.model_validate(first.model_dump(mode="python"))
        self.assertEqual(first.content_digest, second.content_digest)
        self.assertEqual(first.content_digest, canonical_digest(first.graph))
        with self.assertRaises(ValidationError):
            AcceptedGraphRevision(
                revision_number=1,
                graph=graph(),
                content_digest="f" * 64,
            )

    def test_accepted_revision_is_deeply_immutable(self) -> None:
        raw = {"ordered": [1, {"safe": True}]}
        candidate = graph().model_copy(
            update={"nodes": (graph().nodes[0].model_copy(update={"configuration": raw}),)}
        )
        # Validation at the acceptance boundary reconstructs and freezes even a
        # model instance produced through Pydantic's unchecked model_copy API.
        accepted = AcceptedGraphRevision(revision_number=1, graph=candidate)
        raw["ordered"].append(3)  # type: ignore[union-attr]
        configuration = accepted.graph.nodes[0].configuration
        self.assertIsInstance(configuration, FrozenDict)
        self.assertEqual(canonical_json(configuration), '{"ordered":[1,{"safe":true}]}')
        with self.assertRaises(TypeError):
            configuration["new"] = "value"  # type: ignore[index]
        with self.assertRaises(TypeError):
            configuration |= {"new": "value"}  # type: ignore[operator]
        with self.assertRaises(ValidationError):
            accepted.revision_number = 2  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
