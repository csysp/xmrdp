# Contributing

Bug reports, fixes, and focused improvements are welcome. Please open an issue before starting any non-trivial work so we can agree on scope.

---

## Getting started

```bash
git clone https://github.com/csysp/xmrdp
cd xmrdp
pip install -e ".[dev]"
pytest
```

---

## Style guide

### General

- Write for the next person reading the code, not for the interpreter.
- Prefer simple and obvious over clever and compact.
- If you need a comment to explain what a line does, consider rewriting the line first.
- One responsibility per function. If a function needs a "and also" in its docstring, split it.

### Python specifics

- Python 3.8+ compatibility is required. No walrus operator, no `match`, no `tomllib` without the 3.11 guard.
- **No new runtime dependencies.** stdlib only. If you genuinely need a third-party package, make the case in the issue first.
- Type annotations on public functions. Skip them on private helpers where they add noise without clarity.
- Use `pathlib.Path` for filesystem paths, not `os.path` string joins.
- Logging via `logging.getLogger("xmrdp.<module>")` — no `print()` in library code.
- Module-level loggers only: `log = logging.getLogger("xmrdp.foo")` at the top of the file.

### Naming

| Thing | Convention |
|-------|------------|
| Modules, functions, variables | `snake_case` |
| Classes | `PascalCase` |
| Constants | `UPPER_SNAKE_CASE` |
| Private helpers | `_leading_underscore` |

### Security

These are not negotiable:

- **No shell=True.** Use `subprocess.run([...])` with an explicit argument list.
- **Validate at the boundary.** Config values, user input, and anything from the network must be validated before use. See `_SAFE_ARG_RE` and `_HOST_RE` in `config.py` for the existing pattern.
- **Constant-time comparisons for secrets.** Use `hmac.compare_digest`, never `==`.
- **No new ports, no new listening sockets** without a documented threat model entry.
- Firewall rule changes are always print-only. Never apply them programmatically.

### Tests

- New behaviour needs a test. Bug fixes should include a regression test.
- Tests live in `tests/`. Mirror the module name: `xmrdp/foo.py` → `tests/test_foo.py`.
- No mocking of filesystem or subprocess unless the alternative is genuinely impractical. Prefer real temp dirs (`tmp_path`) and controlled inputs.

---

## Pull requests

- Keep PRs focused — one logical change per PR.
- Write a clear description of what changed and why.
- `pytest` must pass before opening for review.
- If your change touches security-relevant code, say so explicitly in the PR description.

---

## License

By contributing you agree that your work will be released under the project's [MIT licence](LICENSE).
