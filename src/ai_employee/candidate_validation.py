"""Read a submitted result, or execute its explicit Python transport in an offline copy."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .domain.v2 import DecisionOutcome, PolicyDecision, ProcessRequest
from .serialization import canonical_digest
from .services_v2 import AtomicArtifactStore, LocalProcessExecutor
from .services_v2._common import identifier, now
from .worker_observation import observation_args, prepare_scratch


class _Cancellation:
    def cancelled(self) -> bool:
        return False


def _regular(root: Path, relative: str, limit: int) -> Path:
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
        raise ValueError("candidate result/manifest/script must not traverse symlinks")
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise ValueError("candidate result/manifest/script is missing or exceeds its bound")
    return path


def candidate_result(
    root: Path, *, seconds: float = 20.0, http_fixture: dict[str, Any] | None = None
) -> Any:
    """For explicitly offline tasks only; never execute against a real remote service.

    A trusted, frozen Harness check calls this and validates the returned JSON against
    its public requirements. A successful program exit alone is not correctness.
    """
    root = root.resolve()
    manifest = root / "output/execute.json"
    if not manifest.exists() and not manifest.is_symlink():
        if http_fixture is not None:
            raise ValueError("HTTP protocol validation requires a declared executable candidate")
        return json.loads(_regular(root, "output/result.json", 1_000_000).read_bytes())
    value = json.loads(_regular(root, "output/execute.json", 4096).read_bytes())
    if (
        not isinstance(value, dict)
        or set(value) != {"script"}
        or not isinstance(value["script"], str)
    ):
        raise ValueError("expected an explicit Python execution manifest")
    script = Path(value["script"])
    if (
        script.is_absolute()
        or ".." in script.parts
        or len(script.parts) < 2
        or script.parts[0] != "src"
        or script.suffix != ".py"
    ):
        raise ValueError("execution script must be below src/")
    _regular(root, script.as_posix(), 1_000_000)
    codex = shutil.which("codex")
    if not codex:
        raise ValueError("offline candidate validation requires native Codex sandbox")
    with tempfile.TemporaryDirectory(prefix="fleet-public-validation-") as directory:
        control = Path(directory)
        scratch = prepare_scratch(root, control / "copies", candidate=True)
        # Prevent a stale result included beside a broken/no-op program from passing.
        previous = scratch / "output/result.json"
        if previous.is_symlink():
            raise ValueError("candidate output must not be a symlink")
        previous.unlink(missing_ok=True)
        arguments: tuple[str, ...] = (sys.executable, "-I", str(scratch / script))
        if http_fixture is not None:
            from .http_validation import validate_fixture

            validate_fixture(http_fixture)
            fixture_path = control / "http-fixture.json"
            fixture_path.write_text(json.dumps(http_fixture))
            arguments = (
                sys.executable,
                "-I",
                str(Path(__file__).with_name("http_validation.py")),
                str(fixture_path),
                str(scratch / script),
            )
        artifacts = AtomicArtifactStore(control / "artifacts")
        executor = LocalProcessExecutor(
            (scratch,),
            artifacts,
            executable_paths=tuple(
                dict.fromkeys(
                    (
                        Path(codex).parent,
                        Path(codex).resolve().parent,
                        Path(sys.executable).resolve().parent,
                        Path("/usr/bin"),
                        Path("/bin"),
                    )
                )
            ),
            inherited_environment={"HOME": str(Path.home())},
        )
        request = ProcessRequest(
            id=identifier("candidate-check"),
            run_id="candidate-check",
            created_at=now(),
            argv=(
                codex,
                *observation_args(str(scratch), (), str(root)),
                "sandbox",
                "--",
                *arguments,
            ),
            cwd=".",
            inherit_environment=("HOME",),
            timeout_seconds=seconds,
            stdout_bytes=1_000_000,
            stderr_bytes=1_000_000,
            purpose="offline execution of the declared candidate program",
        )
        decision = PolicyDecision(
            id=identifier("candidate-check-policy"),
            run_id="candidate-check",
            created_at=now(),
            request_digest=request.content_digest or "",
            effective_policy_digest=canonical_digest(
                {
                    "kind": "frozen_public_check_offline_copy",
                    "root": str(root),
                    "scratch": str(scratch),
                    "request": request.content_digest,
                    "network": "disabled",
                }
            ),
            outcome=DecisionOutcome.ALLOW,
            reason_code="frozen_public_check_offline_copy",
        )
        result = executor.execute(request, decision, _Cancellation())
        if result.status != "succeeded":
            detail = ""
            if result.stderr_artifact_digest:
                descriptor = executor.output_descriptor(
                    result.stderr_artifact_digest, "process_stderr", result.id
                )
                with artifacts.open_verified(descriptor) as stream:
                    detail = stream.read(1_000_000)[-2000:].decode("utf-8", "replace")
            code = result.failure.code.value if result.failure else result.status
            raise ValueError(f"offline candidate validation failed: {code}\n{detail}")
        return json.loads(_regular(scratch, "output/result.json", 1_000_000).read_bytes())
