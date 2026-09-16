# Contributing

## Communication and templates

Use English for new issue and pull request titles, descriptions, comments, and
review feedback. Use English when adding or updating content in existing threads;
there is no requirement to translate historical discussions in bulk. Preserve
original diagnostic messages and quotations when necessary, and provide an English
explanation.

Choose the Bug report, Improvement proposal, or Investigation form when opening an
issue. Separate the problem, evidence, root cause, desired outcome, and suggested
approach. Mark causes as confirmed or hypothetical and explain unknowns rather than
guessing. A complete implementation design is not required to file an issue.

Use the pull request template to connect purpose, behavior changes, implementation,
requirements, and validation. Keep small changes concise and write `None` for
inapplicable sections. When using the CLI or API, include the same relevant sections
in the submitted body; web form requirements are not enforced on those paths.

The templates support shared understanding, not merely completed fields. Follow the
[development workflow](docs/development-workflow.md) for requirement traceability,
review, and validation.

## Development

Use Python 3.11 or newer. Keep authority-changing behavior in deterministic runtime
code, preserve strict schemas and stable error codes, and add focused tests. Follow
the setup and complete verification sequence in `docs/development.md`.

For issue-driven implementation and review, follow the shared
[development workflow](docs/development-workflow.md).

Do not commit credentials, private paths, generated model traces, or copied third-party
source. Contributions are accepted under Apache-2.0.
