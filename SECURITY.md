# Security policy

Version 0.1 receives security fixes. Use the repository's **Security** tab and
**Report a vulnerability** to open a private GitHub security advisory. If private
reporting is unavailable, open a public issue containing no vulnerability details and
ask a maintainer for a private contact channel. Never include live credentials or
sensitive user data in a public report.

Fleet's opt-in incident publisher is a public operational channel for a fixed,
sanitized product-failure schema. It is not a vulnerability-reporting channel.
Suspected vulnerabilities, exploit information, and sensitive security details must
never use incident publishing; use the private process above.

The runtime disables unrestricted network and process authority by default; policy
inputs may only add restrictions. See `docs/security.md` for the v0.1 security boundary
and explicit non-capabilities.
