from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.history_corpus import (
    CorpusEnvironment,
    HistoricalStart,
    inspect_candidate,
    inspect_corpus,
    load_task,
    restore_task,
    write_private,
)
from ai_employee.history_reporting import comparison_report
from ai_employee.inspector import _open_read_only_store
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import GraphRunRecord

ENVIRONMENT = CorpusEnvironment(
    identity="deterministic disposable Python fixture; no external service or credentials",
    external_files=(),
    complete=True,
    uncommitted_state_required=False,
    declaration="operator_attested_complete_task_environment",
)


def fixture(tmp_path, monkeypatch, task_class="small-fix"):
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".fleet").mkdir()
    if task_class == "small-fix":
        changes = {"README.md": ("teh example\n", "the example\n")}
        goal = "Correct the typo in README while keeping its meaning."
        check = "from pathlib import Path; assert Path('README.md').read_text() == 'the example\\n'"
    elif task_class == "medium-feature":
        changes = {
            "names.py": (
                "def legacy(): return 7\n",
                "def legacy(): return 7\ndef normalize(name): return name.strip().lower()\n",
            )
        }
        goal = "Implement name normalization and preserve the legacy function."
        check = (
            "import runpy; ns=runpy.run_path('names.py'); "
            "assert ns['normalize'](' A ') == 'a'; assert ns['legacy']() == 7"
        )
    else:
        changes = {
            "api.py": (
                "def legacy(): return 7\n",
                "def legacy(): return 7\ndef format_name(name): return name.strip().lower()\n",
            ),
            "consumer.py": (
                "RESULT = None\n",
                "from api import format_name\nRESULT = format_name(' A ')\n",
            ),
        }
        goal = (
            "Investigate the API and consumer boundary, define consistent name formatting, "
            "and preserve compatibility."
        )
        check = (
            "import sys; sys.path.insert(0, '.'); import api, consumer; "
            "assert api.legacy() == 7; assert consumer.RESULT == 'a'"
        )
    for name, (before, _) in changes.items():
        (repository / name).write_text(before)
    harness = {
        "schema_version": 2,
        "commands": {
            "acceptance-and-regression": {"argv": [sys.executable, "-I", "-B", "-c", check]}
        },
        "paths": {"writable": list(changes), "protected": [".git/**", ".fleet/**"]},
        "verification": {"required": ["acceptance-and-regression"]},
        "worker": {
            "allowed": ["codex_cli"],
            "allowed_strategy_ids": ["baseline"],
            "adaptive_routing": True,
        },
        "budgets": {"wall_seconds": 30.0, "processes": 20},
    }
    (repository / ".fleet/project.json").write_text(json.dumps(harness))
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "base",
        ),
        check=True,
    )
    worker = tmp_path / "deterministic-worker"
    worker.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""
        import difflib
        import json
        import sys
        from pathlib import Path
        sys.path.insert(0, __SOURCE__)
        if '--version' in sys.argv:
            print('deterministic-test-worker/1'); raise SystemExit
        if '--help' in sys.argv:
            print('exec'); raise SystemExit
        from ai_employee.domain import Goal, SemanticTaskProfile
        from ai_employee.task_orchestration import one_node_graph
        prompt = json.load(sys.stdin)
        protocol = prompt['protocol']
        profile = {'schema_version':'1','task_type':'architecture','reasoning_class':'deep',
                   'scope':'multi_component','ambiguity':'low','reasons':['deterministic test']}
        if protocol == 'fleet-semantic-task-assessment/2':
            print(json.dumps(profile)); raise SystemExit
        if protocol == 'fleet-plan-review/2':
            print(json.dumps({'schema_version':'2','findings':[]})); raise SystemExit
        if protocol == 'fleet-proposed-graph/2':
            goal = Goal.model_validate_json(json.dumps(prompt['goal']))
            graph = one_node_graph(goal, graph_id='fresh-plan', node_id='bounded-task',
                required_capabilities=('edit_intent','process'), max_wall_seconds=30.0)
            graph = graph.model_copy(update={'nodes': tuple(node.model_copy(update={
                'semantic_profile': SemanticTaskProfile.model_validate_json(json.dumps(profile))
            }) for node in graph.nodes)})
            print(json.dumps({'schema_version':'2','goal_id':goal.id,'graph':graph.model_dump(mode='json')}))
            raise SystemExit
        assert protocol == 'fleet-worker-proposal/2'
        assert 'Before committing to implementation details' in prompt['instruction']
        changes = __CHANGES__
        patch = ''
        for name, (before, after) in changes.items():
            assert Path(name).read_text() == before  # observe exact start before editing
            patch += f'diff --git a/{name} b/{name}\\n' + ''.join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile='a/'+name, tofile='b/'+name))
        common = {'schema_version':'2','run_id':prompt['run_id'],
                  'created_at':'2026-01-01T00:00:00Z'}
        print(json.dumps({'schema_version':'2','proposals': [{**common,'id':'proposal',
            'worker_id':'deterministic','kind':'edit_intent','reason':'disposable fixture',
            'payload':{**common,'id':'edit','paths':list(changes),
                       'summary':'bounded change','unified_diff':patch},
            'expected_artifact_kinds':['workspace_patch']}],
            'assistant_note':'', 'usage_json':'{}'}))
    """)
        .replace("__SOURCE__", repr(str(Path(cli.__file__).resolve().parents[1])))
        .replace("__CHANGES__", repr(changes))
    )
    worker.chmod(0o755)
    operator = tmp_path / "operator.json"
    operator.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workers": {"codex_cli": {"executable": str(worker)}},
                "routing": {
                    "default_assessment_strategy": "baseline",
                    "strategies": [
                        {
                            "id": "baseline",
                            "backend": "codex_cli",
                            "model": "deterministic-test-model",
                            "effort": "high",
                            "planner_eligible": True,
                            "capabilities": ["edit_intent", "process"],
                            "max_risk": 10,
                        }
                    ],
                },
            }
        )
    )
    database = tmp_path / "history.db"
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_a, **_kw: database)
    return repository, operator, database, goal


def start_history(repository, operator, goal, run_id="historical"):
    assert (
        cli.main(
            [
                "work",
                goal,
                "--repo",
                str(repository),
                "--operator-config",
                str(operator),
                "--profile",
                "lightweight",
                "--strategy",
                "baseline",
                "--plan-only",
                "--run-id",
                run_id,
            ]
        )
        == 0
    )


@pytest.mark.parametrize("task_class", ["small-fix", "medium-feature", "cross-component-design"])
def test_fresh_private_trials_compare_profiles_quality_first(
    tmp_path, monkeypatch, capsys, task_class
):
    repository, operator, database, goal = fixture(tmp_path, monkeypatch, task_class)
    start_history(repository, operator, goal)
    capsys.readouterr()
    with _open_read_only_store(str(database)) as store:
        candidate = inspect_corpus(
            store, ENVIRONMENT, run_ids=("historical",), task_class=task_class
        )[0]
        assert candidate.classification == "REPRODUCIBLE", candidate.reason
        task = candidate.task
    path = tmp_path / "private-task.json"
    write_private(path, task)
    assert path.stat().st_mode & 0o077 == 0
    assert load_task(path) == task
    payload = json.loads(path.read_text())
    assert "graph" not in payload and "worker_result" not in payload
    for trial_id, profile, guidance in (
        ("lightweight", "lightweight", "on"),
        ("adaptive", "adaptive", "on"),
        ("adaptive-off", "adaptive", "off"),
    ):
        argv = [
            "corpus",
            "run",
            "--fixture",
            str(path),
            "--directory",
            str(tmp_path / trial_id),
            "--operator-config",
            str(operator),
            "--profile",
            profile,
            "--run-id",
            trial_id,
            "--minimal-sufficient",
            guidance,
            "--execute",
        ]
        if profile == "lightweight":
            argv.extend(("--strategy", "baseline"))
        assert cli.main(argv) == 0
        emitted = json.loads(capsys.readouterr().out)
        assert emitted["status"] == "ready_to_promote"
    with _open_read_only_store(str(database)) as store:
        report = comparison_report(store, task, ("lightweight", "adaptive"))
        assert report["matching_recorded_controls"] is True
        trials = report["trials"]
        assert all(item["quality"]["independently_accepted"] is True for item in trials)
        assert all(item["human_active_seconds"] is None for item in trials)
        assert all(item["complexity_scope"]["patch_bytes"] > 0 for item in trials)
        assert all(len(item["profile"]["timings"]) == 3 for item in trials)
        assert trials[0]["usage"]["invocations"] == 1
        assert trials[1]["usage"]["invocations"] > trials[0]["usage"]["invocations"]
        before = store._connection.total_changes
        assert comparison_report(store, task, ("lightweight", "adaptive")) == report
        assert store._connection.total_changes == before
        ablation = comparison_report(store, task, ("adaptive", "adaptive-off"))
        assert ablation["matching_recorded_controls"] is True
        assert ablation["trials"][1]["profile"]["choice"]["minimal_sufficient_guidance"] is False
        assert ablation["trials"][1]["quality"]["independently_accepted"] is True
        # Actual worker-thread transport must receive OFF; source task data is untouched.
        from ai_employee.domain.v2 import ArtifactDescriptor
        from ai_employee.services_v2 import AtomicArtifactStore

        artifacts = AtomicArtifactStore(tmp_path / "artifacts")
        prompts = store.list_records("artifact_descriptor_v2", ArtifactDescriptor)
        worker_prompts = []
        for descriptor in prompts:
            if descriptor.logical_kind != "worker_request":
                continue
            with artifacts.open_verified(descriptor) as stream:
                body = json.load(stream)
            if (
                body.get("protocol") == "fleet-worker-proposal/2"
                and body.get("graph_run_id") == "adaptive-off"
            ):
                worker_prompts.append(body)
        assert len(worker_prompts) == 1
        assert "minimal_sufficient" not in worker_prompts[0]["instruction"]
        assert "Simplicity is a positive" not in worker_prompts[0]["instruction"]
    with SQLiteStore(database) as store:
        store._connection.execute(
            "DELETE FROM records WHERE kind='historical_start_v2' AND run_id='lightweight'"
        )
        store._connection.commit()
        legacy_run = store.get("graph_run_v2", "lightweight", GraphRunRecord)
        recovered = inspect_candidate(store, legacy_run, ENVIRONMENT)
        assert recovered.classification == "REPRODUCIBLE", recovered.reason
        assert recovered.task.start.origin == "recovered_exact_records"
        assert recovered.task.start.runtime_source_digest is None
        # Missing exact independent evidence cannot remain a claimed quality pass.
        store._connection.execute(
            "DELETE FROM records WHERE kind='parent_candidate_evaluation_v2' "
            "AND run_id='lightweight'"
        )
        store._connection.commit()
        rejected = comparison_report(store, task, ("lightweight", "adaptive"))
        assert rejected["trials"][0]["quality"]["independently_accepted"] is None
    assert subprocess.check_output(("git", "-C", str(repository), "status", "--porcelain")) == b""


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("base", "BASE_COMMIT_UNAVAILABLE"),
        ("repo", "REPOSITORY_UNAVAILABLE"),
        ("config", "CONFIG_DIGEST_MISMATCH"),
        ("dirty", "UNCOMMITTED_START_STATE"),
        ("environment", "EXTERNAL_FIXTURE_MISSING"),
    ],
)
def test_reproducibility_rejects_missing_or_changed_inputs(tmp_path, monkeypatch, change, expected):
    repository, operator, database, goal = fixture(tmp_path, monkeypatch)
    start_history(repository, operator, goal)
    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", "historical", GraphRunRecord)
        environment = ENVIRONMENT
        if change == "base":
            run = run.model_copy(update={"base_commit": "0" * 40})
        elif change == "repo":
            run = run.model_copy(update={"repository": str(tmp_path / "missing")})
        elif change == "config":
            run = run.model_copy(update={"operator_config_digest": "0" * 64})
        elif change == "environment":
            environment = None
        else:
            original = store.get("historical_start_v2", "start-historical", HistoricalStart)
            dirty = HistoricalStart.model_validate(
                {
                    **original.model_dump(mode="python", exclude={"content_digest"}),
                    "dirty_state_digest": "0" * 64,
                }
            )
            store.put("historical_start_v2", dirty, run_id=run.id)
        assert inspect_candidate(store, run, environment).classification == expected


def test_private_export_dedup_and_restore_do_not_execute_workers(tmp_path, monkeypatch, capsys):
    repository, operator, database, goal = fixture(tmp_path, monkeypatch)
    start_history(repository, operator, goal)
    start_history(repository, operator, goal, "retry")
    capsys.readouterr()
    with _open_read_only_store(str(database)) as store:
        candidates = inspect_corpus(store, ENVIRONMENT, run_ids=("historical", "retry", "missing"))
    assert [item.classification for item in candidates] == [
        "REPRODUCIBLE",
        "DUPLICATE_TASK",
        "INSUFFICIENT_PROVENANCE",
    ]
    task = candidates[0].task
    output = tmp_path / "task.json"
    write_private(output, task)
    with pytest.raises(FileExistsError):
        write_private(output, task)
    restored = restore_task(task, tmp_path / "restored")
    assert (restored / "README.md").read_text() == "teh example\n"
    with pytest.raises(ValueError, match="absent"):
        restore_task(task, restored)
    with pytest.raises(ValueError, match="--execute"):
        cli.main(
            [
                "corpus",
                "run",
                "--fixture",
                str(output),
                "--directory",
                str(tmp_path / "not-created"),
                "--operator-config",
                str(operator),
                "--profile",
                "lightweight",
                "--strategy",
                "baseline",
                "--run-id",
                "not-run",
            ]
        )
    assert not (tmp_path / "not-created").exists()
    assert cli.main(["corpus", "inspect", "--history-db", str(database)]) == 0
    projection = capsys.readouterr().out
    assert goal not in projection and str(repository) not in projection
    assert "EXTERNAL_FIXTURE_MISSING" in projection
    tampered = json.loads(output.read_text())
    tampered["start"]["goal"]["statement"] = "Different task"
    output.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="content_digest"):
        load_task(output)
    link = tmp_path / "symlink-restore"
    link.symlink_to(tmp_path / "absent-target")
    with pytest.raises(ValueError, match="absent"):
        restore_task(task, link)
    assert not (tmp_path / "absent-target").exists()


def test_corrupt_history_is_rejected_without_hiding_other_tasks_or_exposing_bodies(
    tmp_path, monkeypatch, capsys
):
    repository, operator, database, goal = fixture(tmp_path, monkeypatch)
    start_history(repository, operator, goal)
    capsys.readouterr()
    with SQLiteStore(database) as store:
        store._connection.execute(
            "INSERT INTO records(kind,record_id,run_id,revision,payload) VALUES(?,?,?,?,?)",
            ("graph_run_v2", "broken", "broken", 1, '{"goal":"PRIVATE-HISTORY-CANARY"}'),
        )
        store._connection.commit()
    assert cli.main(["corpus", "inspect", "--history-db", str(database)]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE-HISTORY-CANARY" not in output
    candidates = {item["run_id"]: item for item in json.loads(output)}
    assert set(candidates) == {"historical", "broken"}
    assert candidates["broken"]["classification"] == "INSUFFICIENT_PROVENANCE"
