# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue, through
[Report a vulnerability](https://github.com/Padmapriyan27/frappex-client/security/advisories/new)
on GitHub. Include the affected version, what an attacker can do, and steps to
reproduce. You will get an acknowledgement, and a fix is released before the
details are published.

## Supported versions

Only the latest release receives security fixes.

## What frappex-client guarantees

- Credentials are sent only to the host given in `base_url`: requests use
  site-relative paths and redirects are never followed.
- `http://` is refused unless `allow_insecure_http=True` is passed.
- TLS certificate verification cannot be turned off through the client's
  arguments: a boolean `verify` is refused, and so is an `ssl.SSLContext`
  that does not require certificates and check hostnames. A caller who
  supplies their own `transport` takes over TLS.
- Secrets are not written to logs, `repr()` output or exception messages.
  Request bodies, which can hold a login password, are kept out of `repr()`,
  so error trackers that record local variables do not capture them.
- DocType and document names are encoded, and method names must be dotted
  identifiers, so they cannot address a different endpoint. Downloads accept
  only Frappe's file locations, `/files/...` and `/private/files/...`: a
  `file_url` taken from document data cannot point the client's credentials
  at an API route.
- Replies are bounded: by `max_response_bytes` (checked against the declared
  length and while reading), by `max_request_time` per attempt (so a site
  sending a reply very slowly cannot hold a worker; in the sync client this
  covers the body, while headers are bounded by the read timeout and the HTTP
  library's header size limit), and by offering and accepting only gzip and
  deflate compression. JSON nesting and huge numbers are handled
  without unbounded recursion or precision loss.
- A client created with `read_only=True` sends only GET and HEAD requests
  for data; anything else raises `ReadOnlyError` before it is sent. Password
  login and logout are still sent, and a whitelisted method called by GET
  runs as its author wrote it, so it is a guard for your code rather than a
  permission: the API user's roles on the site remain the real limit.

## Verifying a release

Releases are built and published by GitHub Actions only: PyPI receives them
through Trusted Publishing with a provenance attestation, and from 0.3.0
the files are also signed with Sigstore and attached, with their
`.sigstore.json` bundles, to the GitHub release. To check that a file came
from this repository's release workflow:

```bash
python -m pip install sigstore
python -m sigstore verify github \
  --cert-identity "https://github.com/Padmapriyan27/frappex-client/.github/workflows/release.yml@refs/tags/v0.3.0" \
  frappex_client-0.3.0-py3-none-any.whl
```

## Your responsibilities

- Keep the `httpx` logger at WARNING or above in production: at INFO it logs
  full URLs, and query strings carry filters and method arguments.
- Give each integration its own API user with only the roles it needs.

## Out of scope

frappex-client does not sanitise the data a site returns. Server messages and
document fields can contain HTML written by other users; escape them before
rendering.

## How the project is checked

Every change runs these checks in CI:

| Check | Purpose |
|---|---|
| `tests/unit/test_security.py` | Property-based tests of each guarantee above |
| ruff security rules, CodeQL, Semgrep | Static analysis of the source |
| pip-audit, dependency review, Dependabot | Known vulnerabilities in dependencies |
| Gitleaks, GitHub secret scanning | Committed credentials |
| zizmor, OpenSSF Scorecard | CI configuration and supply-chain practices |

Releases are published to PyPI through Trusted Publishing with signed
provenance attestations. No long-lived upload token exists.
