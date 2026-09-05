from pathlib import Path
from unittest.mock import Mock

import pytest

import ai_employee.productivity_protocol as protocol


def _arguments(*command: str) -> list[str]:
    return [
        "--manifest",
        "manifest.json",
        "--protocol",
        "codex-direct",
        "--task",
        "task.json",
        "--repository",
        "repository",
        "--output-root",
        "output",
        "--timeout",
        "10",
        "--network",
        "disabled",
        *command,
    ]


def test_public_module_cli_strips_remainder_separator(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    collector = Mock(return_value=Path("output/artifacts/codex-direct/command.json"))
    monkeypatch.setattr(protocol, "collect_protocol", collector)

    assert protocol.main(_arguments("--", "worker", "--flag")) == 0

    assert capsys.readouterr().out == "output/artifacts/codex-direct/command.json\n"
    assert collector.call_args.kwargs["arm_command"] == ("worker", "--flag")


def test_public_module_cli_accepts_programmatic_remainder_without_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = Mock(return_value=Path("command.json"))
    monkeypatch.setattr(protocol, "collect_protocol", collector)

    assert protocol.main(_arguments("worker")) == 0

    assert collector.call_args.kwargs["arm_command"] == ("worker",)


@pytest.mark.parametrize("command", ((), ("--",), ("--", "--", "worker")))
def test_public_module_cli_rejects_empty_or_duplicated_separator(
    command: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as raised:
        protocol.main(_arguments(*command))

    assert raised.value.code == 2
