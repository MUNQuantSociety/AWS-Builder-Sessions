"""Tests for client.py: analytics, rendering, caching, routing, and headers."""

from __future__ import annotations

import json
import threading
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import client
from client import (
    SECURITY_HEADERS,
    DashboardData,
    MonthSummary,
    category_bars_html,
    category_totals,
    make_server,
    month_of,
    monthly_chart_svg,
    monthly_summaries,
    route,
)
from server import PROCESSED_KEY, STATUS_KEY, ledger_to_csv


def _row(
    transaction_id: str,
    row_date: str,
    amount: str,
    category: str = "FOOD_AND_DRINK",
) -> dict[str, str]:
    """Build a ledger row as csv_to_ledger returns it."""
    return {
        "transaction_id": transaction_id,
        "date": row_date,
        "name": "Test",
        "amount": amount,
        "category": category,
        "pending": "false",
    }


SAMPLE_ROWS = [
    _row("a", "2026-09-05", "-5000.00", "INCOME"),
    _row("b", "2026-09-10", "1200.00", "RENT_AND_UTILITIES"),
    _row("c", "2026-09-12", "147.39", "FOOD_AND_DRINK"),
    _row("d", "2026-10-01", "-5000.00", "INCOME"),
    _row("e", "2026-10-03", "98.10", "RENT_AND_UTILITIES"),
    _row("f", "2026-10-04", "102.15", "FOOD_AND_DRINK"),
    _row("g", "2026-10-06", "400.00", "TRANSFER_OUT"),
    _row("h", "2026-10-07", "-400.00", "TRANSFER_IN"),
    _row("i", "2026-10-09", "1162.10", "GENERAL_MERCHANDISE"),
]


class FakeStore:
    """Store double that counts reads and serves fixed text."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self.reads = 0

    def read_text(self, key: str) -> str | None:
        self.reads += 1
        return self._files.get(key)

    def write_text(self, key: str, text: str) -> None:
        self._files[key] = text


# --- Analytics ----------------------------------------------------------------------


def test_month_of_returns_year_and_month() -> None:
    assert month_of("2026-09-30") == "2026-09"


def test_month_of_rejects_malformed_dates() -> None:
    with pytest.raises(ValueError):
        month_of("2026-13-01")


def test_monthly_summaries_totals_each_month() -> None:
    summaries = monthly_summaries(SAMPLE_ROWS)

    assert summaries == [
        MonthSummary(
            "2026-09", Decimal("5000.00"), Decimal("1347.39"), Decimal("3652.61")
        ),
        MonthSummary(
            "2026-10", Decimal("5000.00"), Decimal("1362.35"), Decimal("3637.65")
        ),
    ]


def test_transfers_are_excluded_from_totals() -> None:
    october = monthly_summaries(SAMPLE_ROWS)[1]

    assert october.spending == Decimal("1362.35")


def test_negative_savings_when_spending_exceeds_income() -> None:
    rows = [
        _row("x", "2026-11-01", "-100.00", "INCOME"),
        _row("y", "2026-11-02", "250.00"),
    ]

    summary = monthly_summaries(rows)[0]

    assert summary.savings == Decimal("-150.00")


def test_monthly_summaries_of_no_rows_is_empty() -> None:
    assert monthly_summaries([]) == []


def test_monthly_summaries_sorted_oldest_first() -> None:
    rows = [_row("late", "2026-11-01", "10.00"), _row("early", "2026-09-01", "10.00")]

    months = [item.month for item in monthly_summaries(rows)]

    assert months == ["2026-09", "2026-11"]


def test_category_totals_sorted_largest_first() -> None:
    totals = category_totals(SAMPLE_ROWS, "2026-10")

    assert totals == [
        ("GENERAL_MERCHANDISE", Decimal("1162.10")),
        ("FOOD_AND_DRINK", Decimal("102.15")),
        ("RENT_AND_UTILITIES", Decimal("98.10")),
    ]


def test_category_totals_ignore_income_and_other_months() -> None:
    totals = dict(category_totals(SAMPLE_ROWS, "2026-09"))

    assert "INCOME" not in totals
    assert totals == {
        "RENT_AND_UTILITIES": Decimal("1200.00"),
        "FOOD_AND_DRINK": Decimal("147.39"),
    }


def test_category_totals_for_empty_month_is_empty() -> None:
    assert category_totals(SAMPLE_ROWS, "2025-01") == []


# --- Rendering ------------------------------------------------------------------------


def test_chart_with_no_data_shows_a_message() -> None:
    assert monthly_chart_svg([]) == "<p>No transactions yet.</p>"


def test_chart_draws_two_bars_per_month() -> None:
    svg = monthly_chart_svg(monthly_summaries(SAMPLE_ROWS))

    assert svg.count("<rect") == 4
    assert svg.startswith("<svg")
    assert "2026-09" in svg and "2026-10" in svg


def test_chart_with_all_zero_amounts_does_not_divide_by_zero() -> None:
    zero = [MonthSummary("2026-09", Decimal(0), Decimal(0), Decimal(0))]

    svg = monthly_chart_svg(zero)

    assert "<rect" in svg


def test_category_bars_scale_to_the_largest_category() -> None:
    html = category_bars_html(
        [("FOOD_AND_DRINK", Decimal(50)), ("RENT_AND_UTILITIES", Decimal(25))]
    )

    assert "width: 100.0%" in html
    assert "width: 50.0%" in html
    assert "Food And Drink" in html


def test_category_bars_with_no_spending_show_a_message() -> None:
    assert category_bars_html([]) == "<p>No spending recorded for this month.</p>"


def test_category_names_are_escaped_in_html() -> None:
    html = category_bars_html([("<script>x</script>", Decimal(5))])

    assert "<script>" not in html.lower()
    assert "&lt;Script&gt;" in html


# --- Cache -----------------------------------------------------------------------------


def test_repeated_reads_inside_ttl_hit_the_store_once() -> None:
    store = FakeStore({PROCESSED_KEY: ledger_to_csv({})})
    now = [0.0]
    data = DashboardData(store, cache_seconds=60, clock=lambda: now[0])

    data.rows()
    now[0] = 30.0
    data.rows()

    assert (
        store.reads == 2
    )  # one CSV read and one status read, both on the first load only


def test_reads_after_ttl_reload_from_the_store() -> None:
    store = FakeStore({PROCESSED_KEY: ledger_to_csv({})})
    now = [0.0]
    data = DashboardData(store, cache_seconds=60, clock=lambda: now[0])

    data.rows()
    reads_after_first = store.reads
    now[0] = 61.0
    data.rows()

    assert store.reads == reads_after_first + 2


def test_status_is_none_when_no_status_recorded() -> None:
    data = DashboardData(FakeStore({}), cache_seconds=60, clock=lambda: 0.0)

    assert data.status() is None
    assert data.rows() == []


def test_status_is_parsed_from_json() -> None:
    store = FakeStore({STATUS_KEY: json.dumps({"state": "ok", "rows": 21})})
    data = DashboardData(store, cache_seconds=60, clock=lambda: 0.0)

    assert data.status() == {"state": "ok", "rows": 21}


# --- Routing ---------------------------------------------------------------------------


@pytest.fixture
def loaded_data() -> DashboardData:
    """Dashboard data backed by the sample ledger and a successful status."""
    files = {
        PROCESSED_KEY: "transaction_id,date,name,amount,category,pending\n"
        + "".join(
            f"{row['transaction_id']},{row['date']},{row['name']},{row['amount']},"
            f"{row['category']},{row['pending']}\n"
            for row in SAMPLE_ROWS
        ),
        STATUS_KEY: json.dumps(
            {"state": "ok", "last_run_utc": "2026-11-26T00:00:00+00:00"}
        ),
    }
    return DashboardData(FakeStore(files), cache_seconds=60, clock=lambda: 0.0)


def test_root_page_shows_the_latest_month_by_default(
    loaded_data: DashboardData,
) -> None:
    response = route("/", {}, loaded_data)

    assert response.status == 200
    assert response.content_type.startswith("text/html")
    assert "2026-10" in response.body
    assert "$3,637.65" in response.body


def test_root_page_honours_the_month_parameter(loaded_data: DashboardData) -> None:
    response = route("/", {"month": ["2026-09"]}, loaded_data)

    assert "$3,652.61" in response.body


def test_root_page_shows_last_sync_status(loaded_data: DashboardData) -> None:
    response = route("/", {}, loaded_data)

    assert "Last sync 2026-11-26T00:00:00+00:00 (state: ok)." in response.body


def test_root_page_with_no_data_still_renders() -> None:
    empty = DashboardData(FakeStore({}), cache_seconds=60, clock=lambda: 0.0)

    response = route("/", {}, empty)

    assert response.status == 200
    assert "No transactions yet." in response.body
    assert "No sync status has been recorded." in response.body


def test_summary_json_lists_every_month(loaded_data: DashboardData) -> None:
    response = route("/api/summary", {}, loaded_data)

    payload = json.loads(response.body)
    assert response.content_type == "application/json"
    assert payload[0] == {
        "month": "2026-09",
        "income": "5000.00",
        "spending": "1347.39",
        "savings": "3652.61",
    }
    assert len(payload) == 2


def test_categories_json_for_a_month(loaded_data: DashboardData) -> None:
    response = route("/api/categories", {"month": ["2026-10"]}, loaded_data)

    payload = json.loads(response.body)
    assert payload["month"] == "2026-10"
    assert payload["categories"][0] == {
        "category": "GENERAL_MERCHANDISE",
        "total": "1162.10",
    }


def test_categories_without_month_returns_no_categories(
    loaded_data: DashboardData,
) -> None:
    payload = json.loads(route("/api/categories", {}, loaded_data).body)

    assert payload == {"month": None, "categories": []}


@pytest.mark.parametrize(
    "month", ["2026-1", "2026-13", "26-10", "2026-02-01", "2026-10; drop"]
)
def test_bad_month_values_return_400(loaded_data: DashboardData, month: str) -> None:
    response = route("/api/categories", {"month": [month]}, loaded_data)

    assert response.status == 400
    assert "month" in json.loads(response.body)["error"]


def test_bad_month_on_the_root_page_returns_400(loaded_data: DashboardData) -> None:
    assert route("/", {"month": ["nope"]}, loaded_data).status == 400


def test_status_route_reports_not_run_when_absent() -> None:
    empty = DashboardData(FakeStore({}), cache_seconds=60, clock=lambda: 0.0)

    response = route("/api/status", {}, empty)

    assert json.loads(response.body) == {"state": "not_run"}


def test_unknown_path_returns_404(loaded_data: DashboardData) -> None:
    assert route("/nope", {}, loaded_data).status == 404


def test_unexpected_error_returns_generic_500_without_details() -> None:
    class BrokenData:
        def rows(self) -> list[dict[str, str]]:
            raise RuntimeError("secret internal detail")

    response = route("/api/summary", {}, BrokenData())  # type: ignore[arg-type]

    assert response.status == 500
    assert json.loads(response.body) == {"error": "internal error"}
    assert "secret" not in response.body


def test_dashboard_html_escapes_status_text() -> None:
    files = {
        STATUS_KEY: json.dumps(
            {"state": "failed", "error": "<img src=x onerror=alert(1)>"}
        )
    }
    data = DashboardData(FakeStore(files), cache_seconds=60, clock=lambda: 0.0)

    body = route("/", {}, data).body

    assert "<img src=x" not in body
    assert "&lt;img" in body


# --- HTTP server -------------------------------------------------------------------------


@pytest.fixture
def running_server(loaded_data: DashboardData) -> Any:
    """Start the real HTTP server on an ephemeral port for one test."""
    server = make_server("127.0.0.1", 0, loaded_data)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_get_root_sends_security_headers(running_server: str) -> None:
    with urllib.request.urlopen(f"{running_server}/", timeout=5) as response:
        headers = dict(response.headers)

    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value


def test_get_summary_over_http(running_server: str) -> None:
    with urllib.request.urlopen(f"{running_server}/api/summary", timeout=5) as response:
        payload = json.loads(response.read())

    assert [item["month"] for item in payload] == ["2026-09", "2026-10"]


def test_post_is_rejected_with_405(running_server: str) -> None:
    request = urllib.request.Request(f"{running_server}/", data=b"x=1", method="POST")

    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=5)

    assert info.value.code == 405


def test_main_refuses_a_negative_cache_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("CACHE_SECONDS", "-1")
    monkeypatch.delenv("BUCKET_NAME", raising=False)

    assert client.main() == 1


def test_main_refuses_a_non_numeric_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_PORT", "eighty")
    monkeypatch.delenv("BUCKET_NAME", raising=False)

    assert client.main() == 1
