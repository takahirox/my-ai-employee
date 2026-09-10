# Browser application input

This small application is a public fixture that can be used as input to a Fleet
Goal. It contains only application files; it does not select a special Fleet
browser evaluator or grant network access.

Prepare any browser binaries and libraries in the explicitly chosen worker image,
and define required acceptance checks in the Run configuration. The worker decides
how to inspect and repair the application inside that environment. See the root
README and `docs/isolated-worker.md` for the supported execution boundary.
