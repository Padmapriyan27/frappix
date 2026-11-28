# Developer guide

For people changing frappex-client itself. To *use* the library, read the
[README](../README.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.10 or newer.

```bash
git clone <repository-url> && cd frappex-client
uv sync
```

Run everything CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

CI also audits the locked dependencies for known vulnerabilities:

```bash
uv export --locked --no-emit-project --output-file requirements-audit.txt
uvx pip-audit --requirement requirements-audit.txt --disable-pip
```

## How the code is organised

The Frappe API rules are written once, as plain functions that never touch
the network. The two clients are thin layers that send what those functions
describe. This is why the sync and async clients cannot drift apart.

```
src/frappex_client/
  __init__.py        Public names. Anything not exported here is private.
  exceptions.py      Public exception tree.
  types.py           Public type aliases (Document, Filters).
  _auth.py           Secret wrapper and the three auth strategies.
  _config.py         base_url validation, extra-header checks, RetryPolicy.
  _protocol.py       Wire format: Request, RawResponse, safe URL paths, decode().
  _operations.py     One function per API call, returning an Operation.
  _base.py           ClientCore: settings shared by both clients.
  _client.py         Client (blocking).
  _async_client.py   AsyncClient (asyncio).
```

Modules starting with `_` are internal and may change in any release.

### Life of a call

`client.get_doc("ToDo", "T1")` goes through these steps:

1. `_operations.get_doc` builds an `Operation`: a `Request` (method, encoded
   path, query, body) plus a `parse` function for the reply.
2. `Client._execute` logs in first if the client uses `PasswordAuth`.
3. `Client._run` sends the request with `_send`, which streams the body and
   enforces the size limit, returning a `RawResponse`.
4. `RetryPolicy` decides whether to wait and try again.
5. `_protocol.decode` turns the `RawResponse` into the JSON object, or raises
   the matching exception.
6. The operation's `parse` picks the result out of that object.

Steps 1, 4, 5 and 6 are pure and shared. Only steps 2 and 3 differ between
the two clients.

## Adding an API call

1. Add a function to `_operations.py` that returns an `Operation`. Build
   paths with `resource_path` or `method_path`; never format a name into a
   URL yourself.
2. Add a method with the same name and arguments to `Client` **and**
   `AsyncClient`. Each is one line: `return self._execute(ops.<name>(...))`,
   with `await` in the async client.
3. Add a test to `tests/unit/test_clients.py` using the `site` fixture. It
   runs once per client automatically.
4. Add a case under `tests/integration/` if the call's server
   behaviour is not obvious.
5. Add the method to the table in the README and a line to `CHANGELOG.md`.

## Security rules for contributors

These are requirements, not preferences. A change that weakens one needs a
stated reason and a test.

- Never add an option that disables TLS verification.
- Never follow redirects.
- Never put a `Secret`'s value in a log line, `repr`, or exception message.
  Call `.reveal()` only where the value goes on the wire.
- Never include query strings, headers or request bodies in log lines or
  transport error messages; they can hold personal data.
- Every name that becomes part of a URL goes through `_protocol`.
- Do not retry writes. `RetryPolicy` is the only place that decides retries.
- New runtime dependencies need a strong reason; the library has one.

## Tests

| Kind | Location | Needs |
|---|---|---|
| Pure logic | `tests/unit/test_pure.py`, `tests/unit/test_operations.py` | Nothing |
| Security guarantees, property-based (Hypothesis) | `tests/unit/test_security.py` | Nothing |
| Both clients against a fake site | `tests/unit/test_clients.py` | Nothing |
| Real Frappe site | `tests/integration/` | A site and an API key |

The `site` fixture in `tests/unit/conftest.py` records every request and
replies from a queue. `site.run(lambda c: c.some_method(...))` runs the call
on the sync or the async client and returns the result either way.

### Integration tests

They are skipped unless these variables are set:

```bash
export FRAPPEX_TEST_URL=http://localhost:8000
export FRAPPEX_TEST_API_KEY=...
export FRAPPEX_TEST_API_SECRET=...
export FRAPPEX_TEST_HOST_HEADER=mysite.localhost   # only for a multi-site bench
export FRAPPEX_TEST_USERNAME=Administrator         # optional: password-login tests
export FRAPPEX_TEST_PASSWORD=...                   # optional
uv run pytest -m integration
```

Use a disposable or development site, never production. The API user needs
the **System Manager** role.

What the tests write, and how it is removed:

| Written | Removed by |
|---|---|
| `ToDo` documents whose description holds a unique marker | The `marker` fixture, after each test |
| `File` documents attached during upload tests | The test itself, in a `finally` block |
| One custom DocType named `Frappex Test <id>` and its documents | The `doctype` fixture, at the end of the run |
| Two Query Reports named `Frappex Report <id>`, and their Prepared Report runs | The `reports` fixture in `test_report.py`, at the end of the module |

The custom DocType is submittable and lets the caller choose document names,
which is what makes the submit, cancel, rename and awkward-name tests
possible on a site with only Frappe installed.

| File | Covers |
|---|---|
| `tests/integration/conftest.py` | Settings, the shared client, the `marker` and `doctype` fixtures |
| `tests/integration/test_documents.py` | CRUD, filters, paging, awkward names, submit, cancel, rename, error mapping |
| `tests/integration/test_methods_files_auth.py` | Method calls, uploads, downloads, wrong credentials, password login, the async client |
| `tests/integration/test_bulk.py` | `insert_many` batches and rollback, `bulk_update` per-document failures |
| `tests/integration/test_report.py` | `run_report` columns, rows, total row, and a prepared report generated by the site's background worker |
| `tests/integration/test_pdf.py` | `download_pdf` and `download_pdf_to`; the site needs `wkhtmltopdf` |

The PDF tests need `wkhtmltopdf` (the patched-Qt `wkhtmltox` build Frappe
recommends) on the machine that runs the site, and the site's host name
(`frappex.test` for the script below) must resolve to that machine, for
example through `/etc/hosts`. CI does both; the package is checksum-pinned.

Known server behaviour the tests allow for: the development server
(`bench serve`) answers HTTP 500 for a missing static file, where a
production site behind nginx answers 404.

### Starting a throwaway site

`scripts/ci/start_frappe_site.sh` builds a fresh Frappe site and serves it.
CI uses it for every supported Frappe version; you can use it locally with
MariaDB on `127.0.0.1:3306` (root/root) and Redis on `127.0.0.1:6379`:

```bash
scripts/ci/start_frappe_site.sh version-15 /tmp/frappex-bench
set -a && . /tmp/frappex-bench/test.env && set +a
uv run pytest tests/integration
```

## Fuzzing

`fuzz/` holds [Atheris](https://github.com/google/atheris) fuzz targets for
the code that handles untrusted input:

| Target | Invariant |
|---|---|
| `fuzz_decode.py` | Any status, headers and body give a dict or a `FrappexError`; tracebacks stay out of `str(error)` |
| `fuzz_paths.py` | Any DocType, document, method name or file URL gives a path on the intended endpoint, or `ConfigurationError` |
| `fuzz_headers.py` | Accepted extra headers never carry line breaks or replace a managed header |
| `fuzz_json.py` | With orjson installed, decoding gives exactly what `json` gives |

CI runs them with ClusterFuzzLite (`.github/workflows/cflite.yml`): five
minutes on the changed code of each pull request, thirty minutes weekly. To
run one locally (Atheris has wheels for Python 3.12 and later):

```bash
uv venv -p 3.12 /tmp/fuzz && uv pip install -p /tmp/fuzz/bin/python atheris orjson -e .
(cd fuzz && /tmp/fuzz/bin/python -I fuzz_paths.py -max_total_time=60)
```

The paths target found its first bug within seconds: a lone surrogate in a
name raised `UnicodeEncodeError` instead of `ConfigurationError`.

## Benchmarks

`benchmarks/client_overhead.py` measures the time the library itself adds
to a call, with the network replaced by an instant in-memory site:

```bash
uv run python benchmarks/client_overhead.py                 # orjson if installed
uv run python benchmarks/client_overhead.py --no-speedups   # standard json
```

Report the best of several runs and compare like with like on the same
machine; a change that does not beat the run-to-run noise is not kept.

## Continuous integration

GitHub is the canonical repository; the GitLab pipeline mirrors the core
checks.

| Workflow | What it does |
|---|---|
| `ci.yml` | Lint, types, unit tests with coverage (minimum 90%) on Python 3.10 to 3.14; oldest supported `httpx`; package build and metadata checks |
| `integration.yml` | Integration suite against real Frappe v15 and v16 sites |
| `security.yml` | Dependency audit, Gitleaks, workflow audit (zizmor), dependency review on pull requests; also weekly |
| `codeql.yml` | CodeQL static analysis |
| `scorecard.yml` | OpenSSF Scorecard |
| `release.yml` | TestPyPI, then PyPI, through Trusted Publishing |
| `.gitlab-ci.yml` | Lint, types, tests, audit, build, Semgrep SAST and secret detection |

Actions are pinned by commit hash and container images by digest;
Dependabot proposes updates weekly.

## Style

- Code and docstrings follow PEP 8 and PEP 257, with Google-style docstring
  sections. `ruff` enforces both.
- Public functions are fully type-annotated; `mypy --strict` must pass.
- Commits and pull request titles follow
  [Conventional Commits](https://www.conventionalcommits.org), such as
  `feat(client): add get_meta` or `fix(protocol): encode '#' in names`.

## Releasing

1. Update `version` in `pyproject.toml` and move the `Unreleased` entries in
   `CHANGELOG.md` under the new version.
2. Run the full checks and the integration tests.
3. Rehearse: run the `Release` workflow by hand (Actions > Release > Run
   workflow). That builds the package and publishes it to TestPyPI only.
4. Merge to `main`, then publish a GitHub release with the tag `v<version>`.
   The workflow checks that the tag matches the package version, publishes
   to TestPyPI and then to PyPI, each with provenance attestations.

One-time setup, on both test.pypi.org and pypi.org: add a trusted publisher
for the project `frappex-client` with the repository `Padmapriyan27/frappex-client`, the
workflow `release.yml`, and the environment `testpypi` or `pypi`. No upload
token is stored anywhere.
