"""Credential-free full process/adapter preflight in a disposable Docker runtime."""

import subprocess
from pathlib import Path

from ai_employee.domain.v2 import DecisionOutcome, PolicyDecision
from ai_employee.model_usage import filter_model_stdout, model_stdout_filter
from ai_employee.services_v2 import AtomicArtifactStore, LocalProcessExecutor
from ai_employee.services_v2._common import now
from ai_employee.worker_adapters import CodexCliWorkerAdapter
from ai_employee.worker_observation import prepare_scratch

root = Path("/tmp/probe-source")
root.mkdir()
(root / "input").mkdir()
(root / "input/data").write_text("public")
subprocess.run(["git", "init", "-q", str(root)], check=True)
subprocess.run(["git", "-C", str(root), "add", "."], check=True)
scratch = prepare_scratch(root, Path("/tmp/probe-scratch"))
wrapper = Path("/tmp/proxy-codex")
wrapper.write_text("""#!/usr/local/bin/python
import os,subprocess,sys
assert 'exec' not in sys.argv[1:], 'This fixture must never call a model'
env=dict(os.environ,HTTP_PROXY='http://model-proxy:3128',HTTPS_PROXY='http://model-proxy:3128')
raise SystemExit(subprocess.call(['/usr/local/bin/codex',*sys.argv[1:]],env=env))
""")
wrapper.chmod(0o700)
artifacts = AtomicArtifactStore(Path("/tmp/probe-artifacts"))
outputs = {}
inner = LocalProcessExecutor(
    (root,),
    artifacts,
    executable_paths=(Path("/tmp"), Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")),
    inherited_environment={"HOME": "/home/agent", "USER": "agent"},
    stdout_storage_filter=lambda r, d: filter_model_stdout("codex_cli", r, d),
    stdout_stream_filter_factory=lambda r: model_stdout_filter("codex_cli", r),
)


class Capture:
    def execute(self, request, decision, cancellation):
        result = inner.execute(request, decision, cancellation)
        for field, kind in [
            ("stdout_artifact_digest", "process_stdout"),
            ("stderr_artifact_digest", "process_stderr"),
        ]:
            digest = getattr(result, field)
            if digest:
                with artifacts.open_verified(inner.output_descriptor(digest, kind, result.id)) as f:
                    outputs[digest] = f.read()
        assert result.status == "succeeded", (
            request.purpose,
            result.failure,
            outputs.get(result.stderr_artifact_digest),
        )
        return result


def allow(request):
    return PolicyDecision(
        id="policy",
        run_id=request.run_id,
        created_at=now(),
        request_digest=request.content_digest,
        effective_policy_digest="0" * 64,
        outcome=DecisionOutcome.ALLOW,
        reason_code="native_fixture",
    )


adapter = CodexCliWorkerAdapter(
    Capture(),
    lambda digest: outputs[digest],
    allow,
    run_id="probe",
    executable=str(wrapper),
    scratch_directory=str(scratch),
    observation_repository=str(root),
    observation_hosts=("127.0.0.1",),
    inherit_environment=("HOME", "USER"),
)
assert adapter.probe().availability != "unavailable"
print("Full adapter preflight passes with native port remapping and inherited model proxy")
