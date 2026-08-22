# Project Harness

The Project Harness is a declarative, repository-owned description of commands, rules,
protected paths, contracts, and verification expectations. Fleet looks for
`.fleet/project.yaml`, `.fleet/project.yml`, then `.fleet/project.json`. Loading is
non-destructive: if none exists, `discover_project_profile()` returns a provisional in-memory
profile and does not write into the project.

Start with `examples/project/.fleet/project.yaml`. Keep commands deterministic and suitable
for the repository's sandbox. Rules inferred from files remain provisional until a maintainer
reviews and writes an explicit profile. Protected paths express boundaries; they do not grant
write authority or replace OS-level sandboxing.

Parse a profile through the public API:

```python
from ai_employee.project import discover_project_profile

profile = discover_project_profile("examples/project")
print(profile.id, profile.commands)
```
