"""iter_list paging against a fake site that really filters, sorts and limits."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from frappex_client import AsyncClient, Client, ConfigurationError, UnexpectedResponseError

URL = "https://erp.example.com"


class Table:
    """A ToDo table served the way Frappe's get_list serves it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[dict[str, str]] = []

    def _match(self, row: dict[str, Any], condition: list[Any]) -> bool:
        field, op, value = condition[-3:]
        if row[field] is None:  # SQL: NULL compares false
            return False
        if op == "=":
            return bool(row[field] == value)
        if op == ">=":
            return bool(row[field] >= value)
        if op == ">":
            return bool(row[field] > value)
        if op == "in":
            return row[field] in value
        raise AssertionError(f"unexpected operator {op}")

    def handle(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        assert params["order_by"] == "creation asc, name asc"
        filters = json.loads(params.get("filters", "[]"))
        assert isinstance(filters, list), "keyset paging sends list filters"
        or_filters = json.loads(params.get("or_filters", "[]"))
        rows = [
            r
            for r in self.rows
            if all(self._match(r, c) for c in filters)
            and (not or_filters or any(self._match(r, c) for c in or_filters))
        ]
        # MariaDB sorts NULL first in ascending order.
        rows.sort(key=lambda r: (r["creation"] is not None, r["creation"] or "", r["name"]))
        start = int(params["limit_start"])
        rows = rows[start : start + int(params["limit_page_length"])]
        fields = json.loads(params.get("fields", '["name"]'))
        if "*" not in fields:
            rows = [{f: r[f] for f in fields} for r in rows]
        return httpx.Response(200, json={"data": rows})


def collect(table: Table, kind: str, **options: Any) -> list[dict[str, Any]]:
    transport = httpx.MockTransport(table.handle)
    if kind == "sync":
        with Client(URL, transport=transport) as client:
            return list(client.iter_list("ToDo", **options))

    async def main() -> list[dict[str, Any]]:
        async with AsyncClient(URL, transport=transport) as client:
            return [row async for row in client.iter_list("ToDo", **options)]

    return asyncio.run(main())


def make_rows(creations: list[int]) -> list[dict[str, Any]]:
    return [
        {
            "name": f"T-{index:04d}",
            "creation": f"2026-11-28 10:00:{creation:02d}.000000",
            "status": "Open" if index % 3 else "Closed",
            "description": f"row {index}",
        }
        for index, creation in enumerate(creations)
    ]


@pytest.fixture(params=["sync", "async"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class TestKeysetPaging:
    # Few distinct creation values, so most rows share one with others.
    @settings(max_examples=60, deadline=None)
    @given(
        creations=st.lists(st.integers(min_value=0, max_value=5), max_size=120),
        page_size=st.integers(min_value=1, max_value=15),
    )
    def test_every_row_once_in_order_despite_ties(
        self, creations: list[int], page_size: int
    ) -> None:
        table = Table(make_rows(creations))
        expected = sorted(table.rows, key=lambda r: (r["creation"], r["name"]))
        for client_kind in ("sync", "async"):
            table.requests.clear()
            rows = collect(table, client_kind, page_size=page_size)
            assert [r["name"] for r in rows] == [r["name"] for r in expected]

    def test_a_tie_group_larger_than_a_page(self, kind: str) -> None:
        table = Table(make_rows([1] * 25 + [2] * 3))
        rows = collect(table, kind, page_size=4)
        assert len(rows) == 28
        assert len({r["name"] for r in rows}) == 28

    def test_requests_seek_instead_of_skipping(self, kind: str) -> None:
        table = Table(make_rows(list(range(10))))
        collect(table, kind, page_size=3)
        assert {r["limit_start"] for r in table.requests} == {"0"}
        assert {r["limit_page_length"] for r in table.requests} == {"3"}
        assert "or_filters" not in table.requests[0]
        after = "2026-11-28 10:00:02.000000"
        assert ["creation", ">=", after] in json.loads(table.requests[1]["filters"])
        assert json.loads(table.requests[1]["or_filters"]) == [
            ["creation", ">", after],
            ["name", ">", "T-0002"],
        ]

    def test_a_huge_tie_group_costs_the_same_per_page(self, kind: str) -> None:
        table = Table(make_rows([7] * 1000))
        rows = collect(table, kind, page_size=100)
        assert len({r["name"] for r in rows}) == 1000
        # 10 full pages and one empty one; every request asks for 100 rows.
        assert len(table.requests) == 11
        assert {r["limit_page_length"] for r in table.requests} == {"100"}

    def test_rows_hold_only_the_fields_asked_for(self, kind: str) -> None:
        table = Table(make_rows([0, 1, 2]))
        assert collect(table, kind, page_size=2) == [
            {"name": "T-0000"},
            {"name": "T-0001"},
            {"name": "T-0002"},
        ]
        rows = collect(table, kind, fields=["description"], page_size=2)
        assert rows == [
            {"description": "row 0"},
            {"description": "row 1"},
            {"description": "row 2"},
        ]

    def test_every_field(self, kind: str) -> None:
        table = Table(make_rows([0, 1]))
        rows = collect(table, kind, fields=["*"])
        assert rows == table.rows

    def test_dict_filters_keep_their_meaning(self, kind: str) -> None:
        table = Table(make_rows([0, 0, 1, 1, 2, 2, 3]))
        rows = collect(
            table, kind, filters={"status": "Open", "name": ["in", ["T-0001", "T-0002", "T-0003"]]}
        )
        assert [r["name"] for r in rows] == ["T-0001", "T-0002"]

    def test_an_empty_table(self, kind: str) -> None:
        assert collect(Table([]), kind) == []


class TestOffsetFallback:
    @pytest.mark.parametrize(
        "options",
        [
            {"order_by": "modified desc"},
            {"fields": ["count(name) as total"]},
            {"fields": ["items.qty"]},
            {"or_filters": [["status", "=", "Open"], ["priority", "=", "High"]]},
        ],
    )
    def test_falls_back_to_offset_paging(self, options: dict[str, Any], kind: str) -> None:
        seen: list[dict[str, str]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"data": [{"name": "A"}]})

        transport = httpx.MockTransport(handle)
        with Client(URL, transport=transport) as client:
            assert list(client.iter_list("ToDo", page_size=5, **options)) == [{"name": "A"}]
        assert seen[0]["limit_start"] == "0"
        assert "creation" not in seen[0].get("filters", "")


class TestPagingFailures:
    def test_a_site_ignoring_the_keyset_stops_with_an_error(self, kind: str) -> None:
        page = [{"name": "A", "creation": "1"}, {"name": "B", "creation": "1"}]
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": page}))
        with Client(URL, transport=transport) as client, pytest.raises(UnexpectedResponseError):
            list(client.iter_list("ToDo", page_size=2))

    def test_rows_without_a_name_are_rejected(self) -> None:
        transport = httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"creation": "1"}]})
        )
        with Client(URL, transport=transport) as client, pytest.raises(UnexpectedResponseError):
            list(client.iter_list("ToDo", page_size=1))

    @pytest.mark.parametrize("page_size", [0, -1, True])
    def test_page_size_is_checked(self, page_size: int) -> None:
        with Client(URL) as client, pytest.raises(ConfigurationError, match="page_size"):
            list(client.iter_list("ToDo", page_size=page_size))


class TestPagerChoices:
    def test_list_filters_keep_their_meaning(self, kind: str) -> None:
        table = Table(make_rows([0, 1, 1, 2]))
        rows = collect(table, kind, filters=[["status", "=", "Open"]], page_size=1)
        assert [r["name"] for r in rows] == ["T-0001", "T-0002"]

    def test_fields_must_be_a_list(self) -> None:
        with Client(URL) as client, pytest.raises(ConfigurationError, match="fields"):
            list(client.iter_list("ToDo", fields="name"))

    @pytest.mark.parametrize(
        ("options", "keyset"),
        [
            ({}, True),
            ({"fields": ["name", "status"]}, True),
            ({"fields": ["*"]}, True),
            ({"order_by": "creation asc, name asc"}, False),
            ({"fields": ["sum(grand_total) as total"]}, False),
        ],
    )
    def test_keyset_is_chosen_only_when_safe(self, options: dict[str, Any], keyset: bool) -> None:
        from frappex_client._operations import ListPager

        settings: dict[str, Any] = {
            "fields": None,
            "filters": None,
            "or_filters": None,
            "order_by": None,
            "page_size": 10,
            "parent": None,
        }
        assert ListPager("ToDo", **{**settings, **options}).keyset is keyset


class TestEdgeCases:
    def test_no_fields_means_names(self, kind: str) -> None:
        table = Table(make_rows([0, 1, 2]))
        assert collect(table, kind, fields=[], page_size=2) == [
            {"name": "T-0000"},
            {"name": "T-0001"},
            {"name": "T-0002"},
        ]

    def test_rows_without_creation_switch_to_offset_paging(self, kind: str) -> None:
        rows = make_rows(list(range(12)))
        for row in rows[:7]:  # written outside Frappe, no timestamp
            row["creation"] = None
        table = Table(rows)
        names = [r["name"] for r in collect(table, kind, page_size=3)]
        expected = sorted(
            rows, key=lambda r: (r["creation"] is not None, r["creation"] or "", r["name"])
        )
        assert names == [r["name"] for r in expected]
        assert len(set(names)) == 12
        assert any(r["limit_start"] != "0" for r in table.requests)

    def test_a_null_creation_after_keyset_pages_still_reads_every_row(self, kind: str) -> None:
        rows = make_rows([1, 1, 2, 3, 4])
        rows.append({"name": "T-legacy", "creation": None, "status": "Open", "description": "x"})
        table = Table(rows)
        names = [r["name"] for r in collect(table, kind, page_size=2)]
        assert sorted(names) == sorted(r["name"] for r in rows)
        assert names[0] == "T-legacy"
