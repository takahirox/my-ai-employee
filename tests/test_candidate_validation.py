import json
from pathlib import Path

import pytest

from ai_employee.candidate_validation import candidate_result


def test_result_json_and_symlink_rejection(tmp_path):
    (tmp_path / "output").mkdir()
    result = tmp_path / "output/result.json"
    result.write_text('{"count":2}')
    assert candidate_result(tmp_path) == {"count": 2}
    result.unlink()
    result.symlink_to(tmp_path / "private")
    (tmp_path / "private").write_text("secret-canary")
    with pytest.raises(ValueError, match="symlink"):
        candidate_result(tmp_path)


@pytest.mark.parametrize(
    "script", ["../outside.py", "/app/program.py", "input/program.py", "src/program.sh"]
)
def test_manifest_does_not_grant_other_paths(tmp_path, script):
    (tmp_path / "output").mkdir()
    (tmp_path / "output/execute.json").write_text(json.dumps({"script": script}))
    with pytest.raises(ValueError, match="below src"):
        candidate_result(tmp_path)


def test_offline_native_program_and_runtime_error(tmp_path):
    import os
    import subprocess

    image = os.environ.get("FLEET_TEST_OBSERVATION_IMAGE")
    if not image:
        pytest.skip("explicit native Docker image required")
    script = r"""
import json,subprocess
from pathlib import Path
from ai_employee.candidate_validation import candidate_result
root=Path('/tmp/fixture');root.mkdir()
for name in ['src','input','output']: (root/name).mkdir()
(root/'input/numbers.json').write_text('[1,2,3]')
program=root/'src/program.py'
program.write_text("import json;from pathlib import Path;"
 "values=json.loads(Path('input/numbers.json').read_text());"
 "Path('output/result.json').write_text(json.dumps({'count':len(values)}))")
(root/'output/execute.json').write_text('{"script":"src/program.py"}')
subprocess.run(['git','init','-q',str(root)],check=True)
subprocess.run(['git','-C',str(root),'add','.'],check=True)
assert candidate_result(root)=={'count':3}
assert not (root/'output/result.json').exists()
program.write_text("raise RuntimeError('fixture execution failed')")
try:candidate_result(root)
except ValueError as e:assert 'fixture execution failed' in str(e)
else:raise AssertionError('bad program accepted')
program.write_text('pass')
(root/'output/result.json').write_text('{"count":3}')
try:candidate_result(root)
except ValueError:pass
else:raise AssertionError('stale result accepted')
print('offline candidate execution, source preservation, runtime error and stale output passed')
"""
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
            "--mount",
            f"type=bind,src={source},dst=/opt/test-source,readonly",
            image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
