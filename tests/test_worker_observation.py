import pytest

from ai_employee.benchmark_adapter import git
from ai_employee.worker_observation import (
    exact_hosts,
    observation_args,
    observation_authority,
    prepare_scratch,
)


@pytest.mark.parametrize(
    "hosts",
    [
        ("*.example.com",),
        ("http://localhost",),
        ("localhost:80",),
        ("localhost", "localhost"),
        ("EXAMPLE.com",),
        ("a..b",),
    ],
)
def test_host_authority_rejects_ambiguous_rules(hosts):
    with pytest.raises(ValueError):
        exact_hosts(hosts)


def test_project_cannot_expand_operator_authority():
    with pytest.raises(ValueError, match="UNAUTHORIZED"):
        observation_authority(("127.0.0.1",), ())
    assert observation_authority(("127.0.0.1",), ("127.0.0.1", "example.com")) == ("127.0.0.1",)
    args = observation_args("/tmp/fixture", ("127.0.0.1",))
    assert "features.network_proxy=true" in args
    assert 'permissions.fleet-observe.network.domains={"127.0.0.1"="allow"}' in args
    assert "--sandbox" not in args


def test_scratch_is_fresh_tracked_copy_without_git_or_untracked_secrets(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "input").mkdir()
    (root / "input/data.json").write_text("[1,2]")
    (root / "source.py").write_text("print(1)")
    git(root, "add", ".")
    (root / "private.env").write_text("secret-canary")
    scratch = prepare_scratch(root, tmp_path / "scratch")
    assert (scratch / "input/data.json").read_text() == "[1,2]"
    assert not (scratch / ".git").exists()
    assert not (scratch / "private.env").exists()
    (scratch / "source.py").write_text("print(2)")
    assert (root / "source.py").read_text() == "print(1)"
    second = prepare_scratch(root, tmp_path / "scratch")
    assert second != scratch and (second / "source.py").read_text() == "print(1)"


def test_native_observation_and_javascript_in_disposable_container():
    """Opt-in integration: no auth, models, external network or host writable mounts."""
    import json
    import os
    import subprocess
    from pathlib import Path

    image = os.environ.get("FLEET_TEST_OBSERVATION_IMAGE")
    if not image:
        pytest.skip("explicit FLEET_TEST_OBSERVATION_IMAGE required")
    script = r'''
import http.server,json,os,subprocess,threading
from pathlib import Path
from ai_employee.worker_observation import observation_args
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.end_headers()
  page=(b"<pre id='result'>loading</pre><script>fetch('/data').then(r=>r.json())"
        b".then(r=>document.getElementById('result').textContent=JSON.stringify(r))</script>")
  self.wfile.write(b'{"value":42}' if self.path=='/data' else page)
 def log_message(self,*args):pass
server=http.server.HTTPServer(('0.0.0.0',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
source=Path('/tmp/source');source.mkdir();(source/'protected').write_text('original')
scratch=Path('/tmp/candidate');scratch.mkdir();(scratch/'input').mkdir();(scratch/'.tmp').mkdir()
probe=r"""
import json,os,subprocess,urllib.request
from pathlib import Path
Path('writable').write_text('ok')
assert not os.access('/tmp/source',os.W_OK)
os.environ['TMPDIR']=str(Path.cwd()/'.tmp')
for path in ['/tmp/source/protected','input/new']:
 try:Path(path).write_text('bad')
 except OSError:pass
 else:raise AssertionError('protected write allowed: '+path)
url=URL
assert json.loads(urllib.request.urlopen(url+'/data',timeout=3).read())=={'value':42}
denied=[(urllib.request.build_opener(),url.replace('127.0.0.1','127.0.0.2')),
        (urllib.request.build_opener(urllib.request.ProxyHandler({})),url)]
for opener,target in denied:
 try:opener.open(target,timeout=3)
 except OSError:pass
 else:raise AssertionError('unapproved connection allowed')
result=subprocess.run(['chromium-headless-shell','--headless','--no-sandbox','--user-data-dir='+str(Path.cwd()/'.browser'),
'--disable-gpu','--disable-dev-shm-usage','--proxy-server='+os.environ['HTTP_PROXY'],'--proxy-bypass-list=<-loopback>','--virtual-time-budget=3000','--dump-dom',url],capture_output=True,text=True,timeout=15)
assert result.returncode==0,result.stderr[-1500:]
assert ('<pre id="result">{&quot;value&quot;:42}</pre>' in result.stdout or
        '<pre id="result">{"value":42}</pre>' in result.stdout),result.stdout
print('native observation, denied egress, protected source and JavaScript passed')
""".replace('URL',repr('http://127.0.0.1:'+str(server.server_port)))
argv=['codex',*observation_args(str(scratch),('127.0.0.1',),str(source)),'--cd',str(scratch),'sandbox','--','python','-I','-c',probe]
result=subprocess.run(argv,cwd=scratch,capture_output=True,text=True,timeout=30)
print(json.dumps({'code':result.returncode,'stdout':result.stdout[-2000:],'stderr':result.stderr[-2000:]}))
assert result.returncode==0
assert (source/'protected').read_text()=='original'
'''
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--security-opt",
            "seccomp=unconfined",
            "--user",
            "1000:1000",
            "--entrypoint",
            "python",
            "-e",
            "PYTHONPATH=/opt/test-source",
            "-e",
            "NO_PROXY=localhost,127.0.0.1",
            "--mount",
            f"type=bind,src={source},dst=/opt/test-source,readonly",
            image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["code"] == 0


def test_disabled_observation_preserves_existing_identity_and_opt_in_changes_it():
    import json

    from ai_employee.benchmark_adapter import make_harness
    from ai_employee.config import OperatorConfig
    from ai_employee.domain import ProjectHarnessV2
    from ai_employee.serialization import operator_config_digest, project_harness_digest

    config = OperatorConfig()
    harness = ProjectHarnessV2.model_validate_json(json.dumps(make_harness(60)))
    # Captured from the unchanged 6091ee0 implementation, before these fields existed.
    assert (
        operator_config_digest(config)
        == "d8d602c747eadc959c81db2b926ea3e86fbf8860a72f663a2687575a0eccbaf2"
    )
    assert (
        project_harness_digest(harness)
        == "1c89e45c0d5c652bd9509c7869316cfd0b0b9888303a3db838da2d644943b62e"
    )
    assert operator_config_digest(
        config.model_copy(update={"worker_observation_hosts": ("127.0.0.1",)})
    ) != operator_config_digest(config)
    assert project_harness_digest(
        harness.model_copy(
            update={"worker": harness.worker.model_copy(update={"scratch_validation": True})}
        )
    ) != project_harness_digest(harness)


@pytest.mark.parametrize("version", ["codex-cli 0.152.0", "unknown"])
def test_observation_preflight_rejects_unverified_cli_before_generation(
    tmp_path, monkeypatch, version
):
    from ai_employee.domain.v2 import WorkerAvailability
    from ai_employee.services_v2._common import now
    from ai_employee.worker_adapters import CliWorkerAdapter, CodexCliWorkerAdapter

    availability = WorkerAvailability(
        id="probe",
        run_id="run",
        created_at=now(),
        adapter="codex_cli",
        executable="codex",
        availability="auth_unknown",
        auth="unknown",
        version=version,
    )
    monkeypatch.setattr(CliWorkerAdapter, "probe", lambda self: availability)
    adapter = CodexCliWorkerAdapter(
        None,
        None,
        None,
        run_id="run",
        scratch_directory=str(tmp_path / "scratch"),
        observation_repository=str(tmp_path / "source"),
        observation_hosts=("127.0.0.1",),
    )
    monkeypatch.setattr(
        adapter, "_execute", lambda *a, **kw: pytest.fail("unsupported CLI dispatched")
    )
    assert adapter.probe().availability == "unavailable"


def test_full_adapter_preflight_with_inherited_model_proxy():
    import os
    import subprocess
    from pathlib import Path

    image = os.environ.get("FLEET_TEST_OBSERVATION_IMAGE")
    if not image:
        pytest.skip("explicit native Docker image required")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--security-opt",
            "seccomp=unconfined",
            "--user",
            "1000:1000",
            "--entrypoint",
            "python",
            "-e",
            "PYTHONPATH=/opt/test-source",
            "--mount",
            f"type=bind,src={root / 'src'},dst=/opt/test-source,readonly",
            "--mount",
            f"type=bind,src={root / 'tests/fixtures/native_observation_preflight.py'},"
            "dst=/tmp/probe.py,readonly",
            image,
            "/tmp/probe.py",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
