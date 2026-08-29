# Browser evaluator example

This fixture combines an ordinary `process.harness` check with a
`browser.playwright` evaluator. Fleet serves files from the candidate workspace at the
declared loopback origin, clicks the button, and records screenshot, console, DOM, and
accessibility artifacts. No development server or external network access is granted.

Install the optional runtime and its isolated Chromium binary before running a graph-first
`fleet work` flow against this directory:

```console
uv sync --extra browser
uv run playwright install chromium
```

The evaluator uses a fresh browser context, rejects redirects and non-loopback or
cross-origin requests, and tears down the page, context, browser, and driver after every
terminal outcome. The Playwright package is optional; projects without browser evaluators do
not import or require it.
