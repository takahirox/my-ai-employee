"""Supported transport/runtime versions, shared by schema, CLI and preflight."""

SUPPORTED_BACKENDS = ("codex",)
CODEX_VERSION = "codex-cli 0.154.0"

# Prepared images install immutable dependencies beneath the existing :minimal
# read grant; wrappers supply application-specific environment without new grants.
NATIVE_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOCAL_SERVICE_DIRECTORY = "/fleet-runtime"
DEPENDENCY_ENVIRONMENT = {
    "dependency_root": "/usr/local/lib",
    "executable_directory": "/usr/local/bin",
    "path": NATIVE_PATH,
    "instructions": "Prepared-image dependencies are read-only. Use image-provided wrappers "
    "for application-specific environment. Dependencies are not source artifacts.",
}


def execution_environment(local_service_storage_mb: int | None) -> dict[str, object]:
    """Describe only operator-selected capabilities; not model-authored grants."""
    return {
        **DEPENDENCY_ENVIRONMENT,
        "local_services": {
            "enabled": local_service_storage_mb is not None,
            "storage_mb": local_service_storage_mb,
            "data_directory": LOCAL_SERVICE_DIRECTORY
            if local_service_storage_mb is not None
            else None,
            "instructions": "When enabled, use command-local loopback TCP. Start services, wait "
            "for readiness, run tests and stop services in the same command. Restart services "
            "for later commands. Runtime files last only within this invocation and are not "
            "captured, including partial artifacts. Independent verification starts empty. "
            "Unix sockets and cross-command background service reachability are unsupported. "
            "Writes to these disposable services are local effects, not external effects. "
            "External endpoints still require separate authority. Existing external-network "
            "mode also permits command-local TCP; this option is not a denial of that mode.",
        },
    }
