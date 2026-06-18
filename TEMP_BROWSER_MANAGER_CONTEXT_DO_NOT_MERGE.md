# Temporary Browser Manager Context

Remove this file before merging the PR. It is session context for review and follow-up, not product documentation.

## Why This Branch Exists

The current browser-harness works unusually well because the LLM sees the actual Python helper surface and can directly control browser/page behavior with very little indirection. The goal of this branch is to preserve that property while adding a tiny lifecycle layer for cases the current harness handles poorly:

- many parallel agents;
- subagents needing either their own browser or a reused parent browser;
- remote/cloud browser creation from inside the harness flow;
- isolated per-browser daemon/runtime/tmp/artifact directories;
- safer cleanup and switching between browser backends.

The important constraint from the discussion was: do not turn the LLM into a browser manager with a complicated control plane. The LLM should see a small set of obvious helpers, then use the existing page helpers exactly as before.

## Final LLM-Facing Interface

The intended surface is:

```python
browser_status()
browser_new(backend="cloud"|"managed", profile="clean", proxy_country=None, reason=None)
browser_list()
browser_switch(browser_id)
browser_close(browser_id=None)
```

After `browser_new(...)` or `browser_switch(...)`, normal browser-harness helpers such as `new_tab`, `page_info`, `capture_screenshot`, `click_at_xy`, `js`, and `cdp` work unchanged.

For cloud browsers, missing auth should produce `cloud-auth-required`; the model should run `browser-harness auth login` and retry. The user logs in online and the API key is stored locally without being printed into chat. If a user directly provides an API key, the safe storage path is `browser-harness auth login --api-key-stdin`, never a command-line argument.

The model does not need to know about sockets, daemon names, runtime dirs, CDP URLs, Browser Use browser IDs, or process cleanup. Those are manager internals.

## Why Python Instead Of Rust

This was switched from the earlier Rust manager direction to Python because browser-harness is already a Python package and the simplest install path matters more than a theoretically cleaner standalone daemon.

Python keeps the end-to-end flow simple:

```bash
uv tool install -e .
browser-harness <<'PY'
print(browser_new(backend="cloud", proxy_country="us"))
new_tab("https://example.com")
print(page_info())
print(browser_close())
PY
```

No separate Rust build, no extra binary distribution problem, and no cross-language install story. The manager daemon is just another Python module/script in the package.

## Architecture

The manager owns browser leases. A lease includes:

- `browser_id`;
- backend type: `cloud` or `managed`;
- per-browser harness daemon name;
- per-browser runtime/tmp/download/artifact/profile dirs;
- CDP endpoint info;
- owner agent and allowed agent ids;
- an active execution lock.

The runtime path is:

```text
LLM code
  -> browser_* helper
  -> manager_client over Unix socket
  -> manager_daemon creates/switches/closes lease
  -> per-browser browser_harness.daemon
  -> existing page helpers talk to that daemon
```

The existing non-manager browser-harness path still works.

## Parallelism Reasoning

The branch tries to handle the obvious 100-agent failure modes:

- manager auto-start is single-flight via a file lock, so concurrent agents should not start competing managers;
- browser ids and daemon names are generated per lease;
- each lease gets isolated runtime/tmp/artifact/profile directories;
- manager registry state is persisted under the manager root;
- browser creation does not hold the global manager lock while slow cloud/local startup happens;
- execution locks are per client process, so two simultaneous `browser-harness` invocations from the same agent do not mutate the same browser at once;
- cross-run close/switch attempts are rejected.

This is still not a full stress-test result. It is the first implementation pass with targeted unit coverage for the scary cases.

## Subagent Model

The harness cannot rely on controlling Codex subagent spawn parameters. The practical design is therefore prompt/interface based:

- default subagent behavior: call `browser_new(...)` and get an isolated browser;
- reuse behavior: parent gives a `browser_id`, subagent calls `browser_switch(browser_id)`;
- if the browser is busy, the manager returns `busy`, and the safe action is to wait or call `browser_new(...)`.

This keeps the LLM-visible protocol minimal and avoids requiring Codex runtime changes.

## Local Browser Note

The VM used for this work must not start local Chrome or Chromium. Local managed-browser code exists, but local startup was intentionally not smoke-tested here.

Cloud/live lifecycle should be tested separately with a Browser Use API key in the environment. Do not commit keys or put them in docs.

OAuth auth was added after this note was first created. Cloud lifecycle can now also be tested after `browser-harness auth login`, which stores a local Browser Use API key outside the repo.

## Verification Done In This Session

Commands run:

```bash
uv run --with pytest pytest -q tests/unit
uv run python -m compileall -q src/browser_harness
```

Result at the time this note was written:

```text
101 passed
```

A no-browser protocol smoke was also run:

- auto-start Python manager;
- `browser_status()` returned `no-active-browser`;
- `browser_list()` returned `[]`;
- test manager was killed afterward.

No local Chrome/Chromium was started.

## What To Review Before Merge

- Decide whether manager mode should be enabled by AST-detecting lifecycle helper calls, env vars only, or both.
- Live-test `browser_new(backend="cloud")` and `browser_close()` with a real Browser Use key.
- Live-test `browser_new(backend="managed")` on a laptop, not the VM.
- Stress-test many parallel agents/processes using the same manager root.
- Decide whether stale lease cleanup needs a sweeper.
- Decide whether profile support should remain `profile="clean"` only for the first version.
- Remove this file before merging.
