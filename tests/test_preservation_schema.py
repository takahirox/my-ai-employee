"""Evaluate the actual provider projections and native decoding of path syntax."""

from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from ai_employee.models import Clarification, Criterion, Plan, Task
from ai_employee.native import decode_response, provider_schema
from ai_employee.semantics import INPUT_PRESERVATION
from ai_employee.stage_contracts import OutputViolation, StageContract, repair_feedback

from .test_autonomous_runtime import clarification, config
from .test_stage_contracts import stream

VALID_PATHS = [
    ".",
    "AGENTS.md",
    "spec/factories",
    ".hidden",
    "..hidden",
    "...",
    "a/.../b",
    "a/b c.rb",
    "a/日本語.rb",
    "a/ ",
    " a ",
    "a\nb",
    "a/#tag+$file:1",
    "a/.hidden",
    "  .",
    "  ..",
    ". ",
    ".. ",
]
INVALID_PATHS = [
    " ",
    " \t\n",
    "spec/factories/channel/channel_instagram.rb substantially?",
    "/input",
    "//input",
    "../input",
    "a/../input",
    "..",
    "./input",
    "a/./b",
    "a/.",
    "a//b",
    "input/",
    "input/*",
    "a?b",
    "a[b",
    "a]b",
    "a\\b",
    "a\x00b",
]


def projected(stage):
    cls = Clarification if stage == "clarification" else Plan
    contract = StageContract.bind(stage, {}, config())
    schema = provider_schema(cls, contract.projection())
    if cls is Clarification:
        reference = schema["properties"]["criteria"]["items"]
    else:
        task_ref = schema["properties"]["tasks"]["items"]["$ref"].rsplit("/", 1)[1]
        reference = schema["$defs"][task_ref]["properties"]["criteria"]["items"]
    # Evaluate the nested reference emitted for this stage, including its real definitions.
    emitted = {**reference, "$defs": schema["$defs"]}
    Draft202012Validator.check_schema(emitted)
    return cls, Draft202012Validator(emitted)


def criterion_payload(paths):
    return {
        "id": "result",
        "description": "preserve",
        "preserved_paths": paths,
        "preservation_mode": "exact",
        "checks": [],
        "outcome": "artifact",
    }


def stage_payload(cls, criterion):
    if cls is Clarification:
        data = clarification().model_dump(mode="json")
        data["criteria"] = [criterion]
    else:
        data = Plan(
            tasks=(
                Task(
                    id="write",
                    description="write",
                    criteria=clarification().criteria,
                    verification_plan="inspect",
                ),
            ),
            result_task="write",
        ).model_dump(mode="json")
        data["tasks"][0]["criteria"] = [criterion]
    return data


@pytest.mark.parametrize("stage", ["clarification", "planning", "recovery"])
@pytest.mark.parametrize("path", VALID_PATHS + INVALID_PATHS)
def test_actual_provider_schema_and_native_decoder_agree_on_path_syntax(stage, path):
    cls, validator = projected(stage)
    value = criterion_payload([path])
    valid = path in VALID_PATHS
    assert validator.is_valid(value) == valid
    payload = stage_payload(cls, value)
    if valid:
        result, _ = decode_response(stream(payload), cls)
        criteria = result.criteria if cls is Clarification else result.tasks[0].criteria
        assert criteria[0].preserved_paths == (path,)  # no silent normalization
    else:
        with pytest.raises(OutputViolation, match=r"^INVALID_PRESERVATION_PATH$"):
            decode_response(stream(payload), cls)


@pytest.mark.parametrize("paths", [["a", "a"]])
def test_documented_runtime_only_boundaries_retain_stable_repair(paths):
    cls, validator = projected("clarification")
    value = criterion_payload(paths)
    assert validator.is_valid(value)
    with pytest.raises(OutputViolation, match=r"^INVALID_PRESERVATION_PATH$"):
        decode_response(stream(stage_payload(cls, value)), cls)
    assert repair_feedback("INVALID_PRESERVATION_PATH", {})["rule"] == INPUT_PRESERVATION


def test_path_schema_does_not_establish_existence_or_change_serialization():
    _, validator = projected("clarification")
    value = criterion_payload(["missing but syntactically valid"])
    assert validator.is_valid(value)
    expected = copy.deepcopy(value)
    del expected["preservation_mode"]  # legacy exact encoding
    assert Criterion.model_validate(value).model_dump(mode="json") == expected


def test_new_syntax_contract_preserves_legacy_acceptance_for_short_paths():
    # Independent compatibility oracle from the retired implementation, including
    # dot prefixes, whitespace, Unicode, path separators and forbidden characters.
    from itertools import product
    from pathlib import PurePosixPath

    from pydantic import TypeAdapter, ValidationError

    from ai_employee.models import Text

    text = TypeAdapter(Text)
    _, validator = projected("clarification")
    for size in range(1, 5):
        for chars in product("a. /?\n日", repeat=size):
            path = "".join(chars)
            try:
                text.validate_python(path)
                nonblank = True
            except ValidationError:
                nonblank = False
            legacy_valid = nonblank and (
                path == "."
                or (
                    not PurePosixPath(path).is_absolute()
                    and str(PurePosixPath(path)) == path
                    and ".." not in PurePosixPath(path).parts
                    and not any(char in path for char in "*?[]\\\x00")
                )
            )
            value = criterion_payload([path])
            assert validator.is_valid(value) == legacy_valid, repr(path)
            try:
                Criterion.model_validate(value)
                valid = True
            except ValidationError:
                valid = False
            assert valid == legacy_valid, repr(path)
