"""Supported transport/runtime versions, shared by schema, CLI and preflight."""

SUPPORTED_BACKENDS = ("codex",)
CODEX_VERSION = "codex-cli 0.154.0"

# Prepared images install immutable dependencies beneath the existing :minimal
# read grant; wrappers supply application-specific environment without new grants.
NATIVE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEPENDENCY_ENVIRONMENT = {
    "dependency_root": "/usr/local/lib",
    "executable_directory": "/usr/local/bin",
    "path": NATIVE_PATH,
    "instructions": "Prepared-image dependencies are read-only. Use image-provided wrappers "
    "for application-specific environment. Dependencies are not source artifacts.",
}
