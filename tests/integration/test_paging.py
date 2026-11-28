"""Keyset paging in iter_list against a real site."""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import Iterator

import pytest

from frappex_client import AsyncClient, Client

from .conftest import OPTIONS, URL, set_creation, token_auth

ROWS = 120
TIED = 60


def like(marker: str) -> dict[str, list[str]]:
    return {"description": ["like", f"%{marker}%"]}


@pytest.fixture(scope="module")
def tied(client: Client) -> Iterator[tuple[str, list[str]]]:
    """ROWS ToDos, TIED of which share one creation timestamp."""
    marker = f"frappex-paging-{uuid.uuid4().hex}"
    names = client.insert_many("ToDo", [{"description": f"{marker} {i:03d}"} for i in range(ROWS)])
    try:
        first = client.get_value("ToDo", "creation", names[0])
        assert first is not None
        set_creation("ToDo", names[:TIED], first["creation"])
        rows = client.get_list("ToDo", fields=["creation"], filters=like(marker), limit=ROWS)
        assert Counter(row["creation"] for row in rows).most_common(1)[0][1] == TIED
        yield marker, names
    finally:
        for name in names:
            client.delete("ToDo", name)


def test_every_row_once_in_the_sites_order(client: Client, tied: tuple[str, list[str]]) -> None:
    marker, names = tied
    # The site's own order for the same query, in one request.
    expected = [
        row["name"]
        for row in client.get_list(
            "ToDo", filters=like(marker), order_by="creation asc, name asc", limit=ROWS + 10
        )
    ]

    # Pages of 10 inside a tie group of 60 rows.
    paged = [row["name"] for row in client.iter_list("ToDo", filters=like(marker), page_size=10)]

    assert len(paged) == len(set(paged)) == ROWS
    assert set(paged) == set(names)
    assert paged == expected


def test_only_the_requested_fields_come_back(client: Client, tied: tuple[str, list[str]]) -> None:
    marker, _ = tied
    keyset = list(
        client.iter_list("ToDo", fields=["description"], filters=like(marker), page_size=25)
    )
    offset = list(
        client.iter_list(
            "ToDo",
            fields=["description"],
            filters=like(marker),
            order_by="creation asc, name asc",
            page_size=25,
        )
    )
    assert all(set(row) == {"description"} for row in keyset)
    assert keyset == offset


def test_async_keyset_paging(tied: tuple[str, list[str]]) -> None:
    marker, names = tied

    async def scenario() -> list[str]:
        async with AsyncClient(URL, token_auth(), **OPTIONS) as live:
            return [
                row["name"]
                async for row in live.iter_list("ToDo", filters=like(marker), page_size=7)
            ]

    assert sorted(asyncio.run(scenario())) == sorted(names)


def test_rows_without_creation_are_still_read_once(client: Client, marker: str) -> None:
    names = client.insert_many("ToDo", [{"description": f"{marker} {i:02d}"} for i in range(20)])
    set_creation("ToDo", names[:5], "NULL")
    rows = client.get_list("ToDo", fields=["name", "creation"], filters=like(marker), limit=50)
    assert sum(row["creation"] is None for row in rows) == 5

    paged = [row["name"] for row in client.iter_list("ToDo", filters=like(marker), page_size=3)]

    assert len(paged) == len(set(paged)) == 20
    assert set(paged) == set(names)


def test_no_fields_gives_names(client: Client, marker: str) -> None:
    names = client.insert_many("ToDo", [{"description": f"{marker} {i}"} for i in range(4)])
    rows = list(client.iter_list("ToDo", fields=[], filters=like(marker), page_size=3))
    assert sorted(row["name"] for row in rows) == sorted(names)
