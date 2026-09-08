import pytest

from ai_employee.http_validation import execute, validate_fixture


def scenario():
    return {
        "steps": [
            {
                "method": "GET",
                "path": "/workflow",
                "status": 200,
                "response_json": {"jobs": [{"id": "one", "amount": 4, "approved": True}]},
            },
            {
                "method": "POST",
                "path": "/commit",
                "request_json": {"id": "one", "amount": 4, "key": "one"},
                "status": 503,
                "response_json": {"status": "retry"},
            },
            {
                "method": "GET",
                "path": "/workflow",
                "optional": True,
                "status": 200,
                "response_json": {
                    "jobs": [{"id": "one", "amount": 4, "approved": True, "committed": True}]
                },
            },
            {
                "method": "POST",
                "path": "/commit",
                "request_json": {"id": "one", "amount": 4, "key": "one"},
                "status": 200,
                "response_json": {"status": "committed"},
            },
        ]
    }


PROGRAM = """import json
from urllib.request import urlopen, Request
from urllib.error import HTTPError
jobs=json.load(urlopen('http://127.0.0.1:8080/workflow'))['jobs']
for job in jobs:
 if not job['approved']:continue
 for attempt in range(3):
  try:
   data=json.dumps(dict(id=job['id'],amount=job['amount'],key=job['id'])).encode()
   req=Request('http://127.0.0.1:8080/commit',data=data,
    headers={'Content-Type':'application/json'})
   json.load(urlopen(req))
   break
  except HTTPError as error:
   if error.code!=503:raise
"""


def test_same_key_retry_and_optional_recovery(tmp_path):
    script = tmp_path / "program.py"
    for program in (PROGRAM, PROGRAM + "\nraise SystemExit(0)"):
        script.write_text(program)
        execute(scenario(), script)
    script.write_text(
        PROGRAM.replace(
            "if error.code!=503:raise",
            "if error.code!=503:raise\n   json.load(urlopen('http://127.0.0.1:8080/workflow'))",
        )
    )
    execute(scenario(), script)


@pytest.mark.parametrize(
    "replacement",
    [
        "break",
        "json.load(urlopen('http://127.0.0.1:8080/workflow'));break",
        "job['id']='new-key'",
        "raise SystemExit(0)",
    ],
)
def test_missing_retry_or_changed_key_cannot_pass(tmp_path, replacement):
    script = tmp_path / "program.py"
    script.write_text(PROGRAM.replace("if error.code!=503:raise", replacement))
    with pytest.raises(AssertionError, match="HTTP protocol"):
        execute(scenario(), script)


def test_invalid_fixture_fails_without_execution(tmp_path):
    with pytest.raises(ValueError):
        validate_fixture({"steps": []})
    from ai_employee.candidate_validation import candidate_result

    (tmp_path / "output").mkdir()
    (tmp_path / "output/result.json").write_text("{}")
    with pytest.raises(ValueError, match="declared executable"):
        candidate_result(tmp_path, http_fixture=scenario())
