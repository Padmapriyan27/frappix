# frappex-client

[![CI](https://github.com/Padmapriyan27/frappex-client/actions/workflows/ci.yml/badge.svg)](https://github.com/Padmapriyan27/frappex-client/actions/workflows/ci.yml)
[![Integration](https://github.com/Padmapriyan27/frappex-client/actions/workflows/integration.yml/badge.svg)](https://github.com/Padmapriyan27/frappex-client/actions/workflows/integration.yml)
[![Security](https://github.com/Padmapriyan27/frappex-client/actions/workflows/security.yml/badge.svg)](https://github.com/Padmapriyan27/frappex-client/actions/workflows/security.yml)
[![CodeQL](https://github.com/Padmapriyan27/frappex-client/actions/workflows/codeql.yml/badge.svg)](https://github.com/Padmapriyan27/frappex-client/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/Padmapriyan27/frappex-client/badge)](https://scorecard.dev/viewer/?uri=github.com/Padmapriyan27/frappex-client)
[![PyPI version](https://img.shields.io/pypi/v/frappex-client?cacheSeconds=3600)](https://pypi.org/project/frappex-client/)
[![Python versions](https://img.shields.io/pypi/pyversions/frappex-client?cacheSeconds=3600)](https://pypi.org/project/frappex-client/)
[![Downloads](https://static.pepy.tech/badge/frappex-client)](https://pepy.tech/project/frappex-client)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A typed REST client for [Frappe](https://frappe.io/framework) and ERPNext sites,
with a blocking client and an `asyncio` client that share one implementation.

**Documentation: <https://frappex-client.zeal.ninja>**: guides, recipes and the full API reference.

frappex-client is an independent community project. It is not affiliated with or
endorsed by Frappe Technologies.

## What it is

Every Frappe and ERPNext site has a REST API: any program can read and write
its documents over HTTP. frappex-client is the Python package for the other end of
that connection, the code that runs **outside** the site and talks to it.

Use it from any Python application (a Flask, FastAPI or Django web app, a script, a worker or a bot) to work with a Frappe site and every app installed on it, including ERPNext, HRMS and your own custom apps.

Instead of building URLs, encoding filters as JSON and decoding Frappe's error
format by hand:

```python
import json

import requests

response = requests.get(
    "https://erp.example.com/api/resource/Customer",
    headers={"Authorization": f"token {key}:{secret}"},
    params={
        "filters": json.dumps({"territory": "India"}),
        "fields": json.dumps(["name", "customer_name"]),
        "limit_page_length": 20,
    },
    timeout=30,
)
response.raise_for_status()
customers = response.json()["data"]
```

you write:

```python
customers = client.get_list(
    "Customer", filters={"territory": "India"}, fields=["name", "customer_name"]
)
```

## Why frappex-client

Frappe publishes no maintained Python client on PyPI. The official
`frappe-client` repository is archived and was never published, so most
projects either install an unmaintained package or write raw HTTP calls and
solve the same problems again:

| Problem with hand-written calls | What frappex-client does |
|---|---|
| No `asyncio` support for FastAPI, bots and other async programs | `AsyncClient` with the same methods as `Client` |
| The API returns 20 rows by default; reading everything needs a loop | `iter_list` walks every page in a stable order |
| Errors arrive as Frappe's own JSON and must be decoded | A typed exception per failure: `NotFoundError`, `PermissionDeniedError`, `ValidationError` and more |
| A retried insert can create a duplicate document | Only safe requests are retried; writes never are |
| TLS checks get switched off and keys end up in logs | HTTPS only, certificate checks always on, secrets masked everywhere |
| No editor help | Fully typed, checked with `mypy --strict` |

It has one dependency (`httpx`) and is tested on every change against real
Frappe 15 and 16 sites.

## Use cases

**Sync another system into ERPNext.** A webshop, CRM or form backend that
creates or updates ERPNext records:

```python
import os

from frappex_client import Client, TokenAuth

erp = Client(os.environ["ERP_URL"], TokenAuth(os.environ["ERP_KEY"], os.environ["ERP_SECRET"]))


def upsert_customer(email: str, values: dict[str, str]) -> None:
    existing = erp.get_value("Customer", "name", {"email_id": email})
    if existing:
        erp.update("Customer", existing["name"], values)
    else:
        erp.insert("Customer", {"email_id": email, **values})
```

**Import records in bulk.** Hundreds of rows in a few requests instead of
one request each:

```python
from frappex_client import BulkOperationError

rows = [{"description": f"Call lead {lead}"} for lead in leads]
try:
    names = erp.insert_many("ToDo", rows)
except BulkOperationError as error:
    print(f"Saved {len(error.completed)}; resume from row {error.failed_at}: {error.error}")
```

**Serve ERP data from an async web app.** One shared `AsyncClient` in a
FastAPI service:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from frappex_client import AsyncClient, TokenAuth


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncClient("https://erp.example.com", TokenAuth("key", "secret")) as erp:
        app.state.erp = erp
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/orders/{name}")
async def order(name: str):
    return await app.state.erp.get_doc("Sales Order", name)
```

**Export data for reports and pipelines.** Every row, without writing a
pagination loop:

```python
import csv

fields = ["name", "customer_name", "territory"]
with open("customers.csv", "w", newline="") as out:
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows(erp.iter_list("Customer", fields=fields))
```

**Pull report data into another tool.** Any Query or Script Report, rows
keyed by field name:

```python
report = erp.run_report("Accounts Receivable", {"company": "Acme", "report_date": "2026-11-30"})
overdue = [row for row in report.rows if row["outstanding_amount"] > 0]
```

**Call your own app's API.** Methods you whitelist in a custom Frappe app,
from scripts, bots or other services:

```python
summary = erp.call("myapp.api.monthly_summary", {"year": 2026}, http_method="GET")
```

**Attach files to documents.** Invoices, scans and reports, private by
default:

```python
with open("invoice.pdf", "rb") as pdf:
    erp.upload_file("invoice.pdf", pdf.read(), doctype="Sales Invoice", docname="SINV-0001")
```

**Send invoices as PDFs.** Any print format, rendered by the site:

```python
with open("SINV-0001.pdf", "wb") as out:
    erp.download_pdf_to("Sales Invoice", "SINV-0001", out, print_format="Standard")
```

**Monitor a site.** A health check needs no credentials:

```python
from frappex_client import Client, FrappexError

try:
    with Client("https://erp.example.com", timeout=5) as site:
        healthy = site.ping() == "pong"
except FrappexError:
    healthy = False
```

### When not to use it

- **Code running inside a Frappe app.** Use `frappe.get_doc` and the rest of
  the server-side API directly. Calling your own site over HTTP is slower,
  needs an API key, and saves changes outside your current transaction.
- **Realtime events.** frappex-client does not speak Socket.IO.
- **Generating background reports.** For a report that runs in the
  background (a prepared report), `run_report` returns the rows of its last
  completed run with the same filters, or raises `PreparedReportError` when
  there is none. It does not start or wait for a run.
- **The `/api/v2` routes.** Planned for a later release.

## Features

- Sync (`Client`) and async (`AsyncClient`) with identical methods
- Bounded concurrency: at most `max_concurrency` requests in flight (10 by default), so parallel code cannot swamp a site
- API key, OAuth bearer token, username/password, or guest access
- Automatic pagination with `iter_list`, by keyset so deep pages stay fast
- Bulk insert and update in batches, with clear partial-failure reporting
- A read-only mode that refuses write requests before they leave your machine
- Query, Script and Custom Reports as columns and rows keyed by field name
- Print formats as PDFs, streamed to a file if you like
- A typed exception for each kind of failure
- Retries only where repeating a request is safe
- Secure defaults: HTTPS only, certificate checks always on, no redirects
  followed, credentials never printed
- Fully type-annotated; one dependency (`httpx`)

Requires Python 3.10+ for your code. Supports Frappe 15 and 16 sites
through the `/api/resource` and `/api/method` routes; the integration suite
runs in CI against both on every change. Frappe 14 is no longer supported.

## Install

```bash
pip install frappex-client
```

For HTTP/2, which lets many concurrent requests share one connection,
install the optional extra and pass `http2=True`:

```bash
pip install "frappex-client[http2]"
```

```python
client = AsyncClient("https://erp.example.com", auth, http2=True)
```

Sites that do not offer HTTP/2 are spoken to over HTTP/1.1.

For faster decoding of large replies, install the `speedups` extra. It adds
[orjson](https://github.com/ijl/orjson) and needs no code change; list and
iteration calls spend about a third less time in the library:

```bash
pip install "frappex-client[speedups]"      # or "frappex-client[http2,speedups]"
```

## Quick start

```python
from frappex_client import Client, TokenAuth

with Client("https://erp.example.com", TokenAuth("api_key", "api_secret")) as client:
    print(client.get_logged_user())

    todo = client.insert("ToDo", {"description": "Call the supplier"})
    client.update("ToDo", todo["name"], {"status": "Closed"})

    for row in client.iter_list("ToDo", fields=["name", "status"], filters={"status": "Open"}):
        print(row["name"])
```

The async client is the same, awaited:

```python
import asyncio
from frappex_client import AsyncClient, TokenAuth


async def main() -> None:
    async with AsyncClient("https://erp.example.com", TokenAuth("key", "secret")) as client:
        async for row in client.iter_list("Customer", fields=["name"]):
            print(row["name"])


asyncio.run(main())
```

## Authentication

| Class | Use it for |
|---|---|
| `TokenAuth(api_key, api_secret)` | Servers, scripts and jobs. Recommended. |
| `BearerAuth(token)` | An OAuth 2 access token issued by the site. |
| `PasswordAuth(username, password)` | A cookie session. Logs in on first use, logs out on close. |
| *(omitted)* | Guest access. No credentials are sent. |

Read credentials from the environment or a secret store, never from source code.

Without `auth`, the client acts as a guest. That is enough for a health check,
public files and methods the site whitelists with `allow_guest`:

```python
from frappex_client import Client

with Client("https://erp.example.com") as client:
    assert client.ping() == "pong"
```

## Methods

| Method | What it does |
|---|---|
| `ping()` | Check the site answers. |
| `get_logged_user()` | Return the authenticated user. |
| `get_list(doctype, fields=, filters=, or_filters=, order_by=, limit=, offset=)` | One page of documents. |
| `iter_list(doctype, ..., page_size=)` | Every matching document, page by page. |
| `get_doc(doctype, name)` | One document. |
| `insert(doctype, values)` | Create a document. |
| `update(doctype, name, values)` | Change fields. |
| `delete(doctype, name)` | Delete a document. |
| `insert_many(doctype, docs, batch_size=)` | Create many documents, up to 200 per request; returns their names. |
| `bulk_update(doctype, {name: values}, batch_size=)` | Change many documents; returns the ones the site reported an error for. |
| `submit(doctype, name)` / `cancel(doctype, name)` | Submit or cancel. |
| `rename(doctype, old_name, new_name, merge=)` | Rename; returns the new name. |
| `get_count(doctype, filters)` | Count matching documents. |
| `get_value(doctype, fieldname, filters)` / `set_value(doctype, name, values)` | Read or set fields. |
| `call(method, params, http_method=)` | Call a whitelisted method. |
| `run_report(report_name, filters)` | Run a Query, Script or Custom Report; returns columns, rows and the totals row. |
| `upload_file(filename, content, is_private=, doctype=, docname=)` | Upload a file. |
| `download(file_url)` / `download_to(file_url, target)` | Fetch a file. |
| `download_pdf(doctype, name, print_format=)` / `download_pdf_to(doctype, name, target)` | Render a document's print format as a PDF. |

## Errors

```python
from frappex_client import NotFoundError, PermissionDeniedError, ValidationError, FrappexError

try:
    client.get_doc("Customer", "CUST-0001")
except NotFoundError:
    ...
except PermissionDeniedError:
    ...
except FrappexError as error:  # everything frappex-client raises
    ...
```

| Exception | When |
|---|---|
| `AuthenticationError` | 401: bad or expired credentials |
| `PermissionDeniedError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError` | 409: duplicate or conflicting write |
| `ValidationError` | 417 and other 4xx |
| `RateLimitError` | 429; `retry_after` holds the advised wait |
| `ServerError` | 5xx |
| `TransportError`, `RequestTimeoutError` | No response was received |
| `UnexpectedResponseError` | Redirect or non-JSON reply (usually a wrong `base_url`) |
| `ResponseTooLargeError` | Body above `max_response_bytes` |
| `ConfigurationError` | An unsafe or malformed argument |

`APIError` subclasses carry `status_code`, `exc_type`, `server_messages` and
`traceback`. `exc_type` is `None` when the reply does not name the
server-side exception, for example an error page from a proxy; rely on the
exception class for control flow. The server traceback is deliberately left
out of `str(error)`.

## Security behaviour

- `http://` is refused unless you pass `allow_insecure_http=True` (local development only).
- TLS verification cannot be disabled. For a private CA, pass `verify="/path/ca.pem"` or an `ssl.SSLContext`.
- Redirects are never followed, so credentials cannot be forwarded to another host.
- DocType and document names are percent-encoded; `.` and `..` are rejected.
- Downloads accept only `/files/...` and `/private/files/...`, so a `file_url`
  from document data cannot make the client call an API route.
- Each request attempt has an overall time limit (`max_request_time`, 300 s
  by default), so a site that sends a reply very slowly cannot hang a worker.
- Only gzip and deflate compression is offered, and `verify` must keep
  certificate and hostname checks on.
- Uploads are private by default.
- Writes are never retried after a network error or 5xx. `GET` requests and HTTP 429 replies are.
- Secrets are masked in `repr()` and never logged.
- `read_only=True` makes a client send only GET and HEAD requests for data:
  inserts, updates, deletes, uploads and POST method calls raise
  `ReadOnlyError` before anything is sent. Useful for reporting and export
  scripts pointed at a live site. Password login and logout are still sent,
  and a whitelisted method you call by GET runs as written, so one that writes
  and accepts GET still writes. The API user's roles on the site remain the
  real limit.

```python
with Client("https://erp.example.com", auth, read_only=True) as erp:
    rows = list(erp.iter_list("Sales Invoice"))  # reads work
    erp.delete("Sales Invoice", rows[0]["name"])  # raises ReadOnlyError, nothing sent
```

**Logging.** frappex-client's own log lines (logger `frappex_client`, at
DEBUG) name the method, path and status, never query strings, bodies or
headers. httpx logs every full URL, query string included, at INFO; filters
and method arguments travel in query strings and can contain personal data,
so keep that logger at WARNING in production:

```python
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
```

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Retries and timeouts

```python
from frappex_client import Client, RetryPolicy, TokenAuth

client = Client(
    "https://erp.example.com",
    TokenAuth("key", "secret"),
    timeout=10,
    retry=RetryPolicy(max_attempts=5, backoff_base=1.0),
)
```

`RetryPolicy(max_attempts=1)` turns retries off.

`timeout` applies to each network read. `max_request_time` (300 s by
default) bounds a whole request attempt, so a site that sends a reply very
slowly cannot hold a worker, and `max_concurrency` (10 by default) caps how
many requests are in flight at once.

**Large data in async code.** Decoding a reply takes CPU time during which
the event loop cannot run other tasks: about 5 ms per MiB of JSON. Python's
JSON decoders hold the interpreter lock while they work, so moving decoding
to a thread does not avoid this. Read large tables with `iter_list` (100 rows,
about 150 KiB, per request by default) instead of one huge `get_list`, and
lower `page_size` if latency matters more than throughput.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Integration tests run against a live development site and are skipped unless
these are set. They remove everything they create. See the
[developer guide](docs/DEVELOPMENT.md#integration-tests) for the full list of
settings and the permissions the API user needs:

```bash
export FRAPPEX_TEST_URL=http://localhost:8000
export FRAPPEX_TEST_API_KEY=...
export FRAPPEX_TEST_API_SECRET=...
uv run pytest -m integration
```

## Documentation

- [Documentation website](https://frappex-client.zeal.ninja): guides, recipes and the API reference
- [Developer guide](docs/DEVELOPMENT.md): architecture, adding a call, tests, releasing
- [Product requirements](docs/PRD.md): goals, scope and requirements
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

MIT
