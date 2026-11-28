# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `read_only=True` on `Client` and `AsyncClient` refuses every request that
  could change data (inserts, updates, deletes, submit, cancel, rename,
  `set_value`, bulk writes, uploads and methods called by POST) with the new
  `ReadOnlyError`, before anything is sent. Reads, `call(..., http_method="GET")`,
  reports and downloads work, and password login and logout are still sent.
  A whitelisted method called by GET runs as written, so one that writes and
  accepts GET still writes: the API user's roles remain the real limit. The
  `read_only` property reports the setting.

- `max_concurrency` on `Client` and `AsyncClient` (10 by default) caps how
  many requests are in flight at once, including downloads. Further
  requests wait for a free slot instead of failing, a slot is not held
  while waiting to retry, and the connection pool is sized to match.
  `max_concurrency=None` removes the limit. The `max_concurrency` property
  reports the setting.

- Opt-in HTTP/2: `pip install "frappex-client[http2]"` and pass
  `http2=True` to `Client` or `AsyncClient`. Concurrent requests then share
  one connection; sites that do not offer HTTP/2, and plain `http://` sites,
  are spoken to over HTTP/1.1. Without the extra, `http2=True` raises
  `ConfigurationError` saying what to install. The default install still
  depends only on `httpx`.
- Opt-in faster JSON decoding: `pip install "frappex-client[speedups]"`
  adds orjson (3.11.6 or later, free of known advisories), which is then
  used automatically. In the benchmark
  (`benchmarks/client_overhead.py`), `get_list` of 100 rows and `iter_list`
  spend about 35% less time in the library. Results are always identical to
  the standard library: replies orjson would read differently (integers
  beyond 64 bits, which it rounds, a lone surrogate, `NaN`, very deep
  nesting) are decoded with `json`.
- Debug log lines name the protocol each request used, for example
  `GET /api/method/ping -> 200 (HTTP/2)`.

### Fixed

- A `PasswordAuth` session that expires on the site is renewed: on a 401 or
  403 the client asks the site who is logged in, and if the answer is a
  guest it logs in again once and repeats the request once (safe for
  writes, since a request refused as unauthenticated never ran). A genuine
  permission error is raised unchanged, without a second login. Concurrent
  callers renew the session only once, and requests whose refusal arrives
  while another caller is logging in again wait for that login. Before, long-running programs using
  `PasswordAuth` failed with `AuthenticationError` for good once the session
  timed out.

### Security

- Downloads (`download`, `download_to`) accept only Frappe's file locations,
  `/files/...` and `/private/files/...`, without empty segments (`//`) or
  control characters. Before, a `file_url` taken from
  document data could point at any route on the site, such as
  `/api/resource/User/Administrator`, which the client would then call with
  its own credentials.
- Request bodies are no longer part of `repr()`, and an error raised while
  logging in carries no chained network-library exception. A failed
  password login, whether refused or cut off by a network error, left the
  password in local variables that error trackers which capture them
  (Sentry and others) would record.
- An `ssl.SSLContext` passed as `verify` must require certificates and check
  hostnames; one that does not is refused, like `verify=False`.
- Only `gzip, deflate` is offered in `Accept-Encoding`, the header can no
  longer be overridden, and a reply that uses any other `Content-Encoding`
  is refused before its body is read (a hostile site could otherwise send
  brotli or zstd regardless). httpx would otherwise offer brotli or zstd when
  those packages are installed, which can expand a small reply into
  gigabytes before the size limit is checked.
- New `max_request_time` (300 s by default, or twice the read timeout if
  longer; `math.inf` disables it): the longest one request attempt may take.
  Per-read timeouts alone never stop a site that sends a reply a byte at a
  time. The async client enforces it over the whole request, headers
  included. The sync client checks it each time part of the body arrives,
  so at worst it ends one read timeout late; its header phase stays bounded
  by the read timeout and the HTTP library's header size limit.
- A DocType name, document name or file URL that is not valid Unicode (a
  lone surrogate) raises `ConfigurationError` instead of `UnicodeEncodeError`
  from inside the URL encoder. Found by fuzzing.
- Requires Python 3.10.7 or later, the first 3.10 release that limits
  conversion of huge numbers (CVE-2020-10735).

### Changed

- **Downloads are limited to `/files/` and `/private/files/`** (see Security).
- **Each request attempt is limited to `max_request_time`** (see Security).
- **At most 10 requests are in flight at once by default.** Before, an
  `asyncio.gather` of hundreds of calls opened up to 100 connections to the
  site. Pass `max_concurrency=None` for the old behaviour.
- `iter_list` pages by keyset with its default order and plain field names:
  each request asks for rows created at or after the last one seen, instead
  of skipping `offset` rows. Every page costs the same however deep the
  iteration goes (offset paging makes the database step over every earlier
  row), and rows created or deleted during the iteration no longer shift
  pages. Rows sharing a `creation` timestamp are handled exactly. A custom
  `order_by`, computed or child-table fields, or caller `or_filters` keep
  offset paging. The rows returned and their order are unchanged. Each
  page asks for the rows after the last `(creation, name)` pair, so rows
  that share a timestamp (child rows, bulk imports) cost the same as any
  other. Rows with no `creation` (written outside Frappe) sort first and
  are read by offset paging, and `fields=[]` returns names as before.

## [0.2.0] - 2026-11-26

### Removed

- **Frappe 14 is no longer supported.** frappex-client now supports and
  tests the two current major versions, Frappe 15 and 16, on every change.
  Sites on Frappe 14 can keep using 0.1.1. Your own code can still run on
  Python 3.10 or later.

### Added

- `insert_many(doctype, docs, batch_size=200)` creates many documents in
  batches through `frappe.client.insert_many`. Each batch is saved
  completely or not at all, and the names come back in the order given.
- `bulk_update(doctype, {name: values}, batch_size=200)` changes many
  documents through `frappe.client.bulk_update`. Frappe saves each document
  separately; the ones it reported an error for are returned as
  `BulkUpdateFailure` items with the server's message. Frappe does not roll
  such a document back, so changes written before the error can remain.
- `BulkOperationError`, raised when a whole batch fails. It records the
  names already saved (`completed`), where to resume (`failed_at`) and the
  original exception (`error`). Like other writes, a batch is not retried
  after a network or server error.
- `run_report(report_name, filters=None)` runs a Query, Script or Custom
  Report through `frappe.desk.query_report.run` and returns a `ReportResult`:
  normalised `columns`, `rows` keyed by field name (old `"Label:Type/Options"`
  columns and list rows included), the `total_row` kept apart, the report's
  `message` and the `raw` reply. Filters you pass always apply, even to a
  Custom Report with saved filters. For a report that runs in the
  background (a prepared report), it returns the rows of the last completed
  run with the same filters and sets `prepared`.
- `PreparedReportError`, raised when a prepared report has no completed run
  for the filters, or its last run failed (on Frappe 16 the reason is in
  `error`),
  instead of returning a job status as if it were data.
- `download_pdf(doctype, name, ...)` and `download_pdf_to(doctype, name,
  target, ...)` render a document's print format as a PDF through
  `frappe.utils.print_format.download_pdf`, with optional `print_format`,
  `letterhead`, `no_letterhead` and `language`. A reply that is not
  `application/pdf`, such as an HTML page, raises `UnexpectedResponseError`
  instead of being written to the file. Like other downloads, they are not
  retried.

## [0.1.1] - 2026-11-24

### Changed

- **Renamed to `frappex-client`.** Install with `pip install frappex-client`
  and import `frappex_client`. The name `frappix` already belongs to an
  established Frappe community project by another author (Frappe on Nix), so
  this library no longer uses it. There are no other changes to the API.
- The base exception is now `FrappexError` (was `FrappixError`).
- Integration test settings are now `FRAPPEX_TEST_*` (were `FRAPPIX_TEST_*`).
- Documentation moved to <https://frappex-client.zeal.ninja>.

### Migrating from frappix 0.1.0

```bash
pip uninstall frappix
pip install frappex-client
```

Then replace `frappix` with `frappex_client` in imports and `FrappixError`
with `FrappexError`. The `frappix` 0.1.0 release on PyPI is yanked.

## [0.1.0] - 2026-11-24

First release, published under the name `frappix`.

### Added

- `Client` and `AsyncClient` with document, method, file and pagination calls.
- `TokenAuth`, `BearerAuth` and `PasswordAuth`.
- Guest access: `auth` is optional, for `ping`, public files and
  `allow_guest` methods.
- Typed exception hierarchy and `RetryPolicy`.
- Unit tests for both clients and an integration suite for a live site,
  run in CI against Frappe 14, 15 and 16.
- Property-based security tests, coverage threshold, and CI security
  scanning (CodeQL, Semgrep, Gitleaks, pip-audit, zizmor, Scorecard).

### Fixed

- `get_value` returns `None`, not an empty mapping, when nothing matches.
- An error reply containing an unpaired surrogate no longer crashes decoding.
- An unsupported `verify` value raises `ConfigurationError`.
- `exc_type` is read from Frappe 14's `exception` field when `exc_type` is
  absent.

[Unreleased]: https://github.com/Padmapriyan27/frappex-client/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Padmapriyan27/frappex-client/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Padmapriyan27/frappex-client/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Padmapriyan27/frappex-client/releases/tag/v0.1.0
