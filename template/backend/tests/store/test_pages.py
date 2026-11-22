"""A page is ordered by one column, so the bytes Postgres adds up follow the order of the page.
`docs/adr/template/0026`."""

import pytest

from app.store.pages import UnorderedPage, fetch_page


@pytest.mark.parametrize("order", ["seq, id", "seq ASC", "lower(title)", "seq; DROP TABLE x"])
async def test_a_page_in_any_order_but_one_column_is_refused(order: str) -> None:
    with pytest.raises(UnorderedPage):
        await fetch_page(None, "SELECT seq FROM tasks", order)
