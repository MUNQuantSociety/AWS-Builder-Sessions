"""Dashboard: read stored transactions and serve the monthly view over HTTP.

Run on EC2 or locally:
    python client.py

Settings come from environment variables. Storage settings are the same as
server.py: BUCKET_NAME, STORE_PREFIX, LOCAL_STORE_DIR. These are also read:

    CACHE_SECONDS      How long to keep data before reloading (default 60)
    DASHBOARD_HOST     Bind address (default 127.0.0.1; use 0.0.0.0 on EC2)
    DASHBOARD_PORT     Port (default 8000)

Routes:
    GET /                     Dashboard page for one month
    GET /api/summary          Monthly income, spending, and savings (JSON)
    GET /api/categories       Spending by category for one month (JSON)
    GET /api/status           Last sync status from server.py (JSON)

The dashboard has no login, so it should only be reachable from a network you
control. The data it shows is sandbox data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from server import (
    PROCESSED_KEY,
    STATUS_KEY,
    Store,
    csv_to_ledger,
    load_settings,
    open_store,
)

LOGGER = logging.getLogger("client")
DEFAULT_CACHE_SECONDS = 60
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8000
MONTH_PATTERN = re.compile(r"\d{4}-\d{2}")
TRANSFER_CATEGORIES = frozenset({"TRANSFER_IN", "TRANSFER_OUT"})
CENT = Decimal("0.01")

CHART_WIDTH = 640
CHART_HEIGHT = 280
CHART_PADDING_LEFT = 56
CHART_PADDING_BOTTOM = 40
CHART_PADDING_TOP = 16
INCOME_COLOR = "#2e7d32"
SPENDING_COLOR = "#c62828"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
PAGE_STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 760px; color: #1a1a1a; }
h1 { font-size: 1.5rem; margin-bottom: 0; }
.notice { color: #8a4b00; font-size: 0.9rem; }
.cards { display: flex; gap: 1rem; margin: 1rem 0; }
.card { flex: 1; border: 1px solid #ddd; border-radius: 6px; padding: 0.75rem; }
.card .value { font-size: 1.25rem; font-weight: 600; }
.categories { list-style: none; padding: 0; }
.category { display: grid; grid-template-columns: 9rem 1fr 6rem; gap: 0.5rem; align-items: center; margin: 0.4rem 0; }
.track { background: #eee; height: 0.8rem; border-radius: 4px; }
.fill { display: block; background: #3d6fb6; height: 100%; border-radius: 4px; }
.amount { text-align: right; font-variant-numeric: tabular-nums; }
.status { color: #555; font-size: 0.9rem; }
"""


# --- Analytics -----------------------------------------------------------------


@dataclass(frozen=True)
class MonthSummary:
    """Totals for one calendar month.

    Attributes:
        month: Month as YYYY-MM.
        income: Total money in, as a positive number.
        spending: Total money out, as a positive number.
        savings: Income minus spending. Negative when spending exceeds income.
    """

    month: str
    income: Decimal
    spending: Decimal
    savings: Decimal


def month_of(row_date: str) -> str:
    """Return the YYYY-MM part of a transaction date.

    Args:
        row_date: Date as YYYY-MM-DD.

    Returns:
        The month as YYYY-MM.

    Raises:
        ValueError: If the date is not a valid YYYY-MM-DD date.
    """
    return date.fromisoformat(row_date).strftime("%Y-%m")


def monthly_summaries(rows: Sequence[dict[str, str]]) -> list[MonthSummary]:
    """Compute income, spending, and savings for each month.

    Plaid reports money leaving an account as a positive amount and money
    arriving as a negative amount. Transfers are excluded from both totals.

    Args:
        rows: Ledger rows with date, amount, and category keys.

    Returns:
        Summaries sorted oldest first.

    Raises:
        ValueError: If a date or amount is malformed.
    """
    income: dict[str, Decimal] = {}
    spending: dict[str, Decimal] = {}
    for row in rows:
        if row["category"] in TRANSFER_CATEGORIES:
            continue
        month = month_of(row["date"])
        amount = Decimal(row["amount"])
        income.setdefault(month, Decimal(0))
        spending.setdefault(month, Decimal(0))
        if amount < 0:
            income[month] -= amount
        else:
            spending[month] += amount

    summaries = []
    for month in sorted(income):
        month_income = _cents(income[month])
        month_spending = _cents(spending[month])
        summaries.append(
            MonthSummary(
                month,
                month_income,
                month_spending,
                _cents(month_income - month_spending),
            )
        )
    return summaries


def category_totals(
    rows: Sequence[dict[str, str]], month: str
) -> list[tuple[str, Decimal]]:
    """Total spending by category for one month, largest first.

    Args:
        rows: Ledger rows with date, amount, and category keys.
        month: Month to total, as YYYY-MM.

    Returns:
        (category, total) pairs sorted by total, descending. Income and
        transfers are excluded.

    Raises:
        ValueError: If a date or amount is malformed.
    """
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row["category"] in TRANSFER_CATEGORIES:
            continue
        amount = Decimal(row["amount"])
        if amount <= 0 or month_of(row["date"]) != month:
            continue
        totals[row["category"]] = totals.get(row["category"], Decimal(0)) + amount
    return sorted(
        ((category, _cents(total)) for category, total in totals.items()),
        key=lambda pair: pair[1],
        reverse=True,
    )


def _cents(value: Decimal) -> Decimal:
    """Round a money value to cents, half up.

    Args:
        value: Value to round.

    Returns:
        The rounded value.
    """
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


# --- Rendering -----------------------------------------------------------------


def monthly_chart_svg(summaries: Sequence[MonthSummary]) -> str:
    """Draw income and spending bars for each month, as inline SVG.

    Args:
        summaries: Monthly summaries, oldest first.

    Returns:
        An SVG element as a string, or a short message if there is no data.
    """
    if not summaries:
        return "<p>No transactions yet.</p>"

    plot_width = CHART_WIDTH - CHART_PADDING_LEFT
    plot_height = CHART_HEIGHT - CHART_PADDING_TOP - CHART_PADDING_BOTTOM
    peak = max(max(item.income, item.spending) for item in summaries)
    scale = plot_height / float(peak) if peak > 0 else 0.0
    group_width = plot_width / len(summaries)
    bar_width = group_width * 0.35
    baseline = CHART_PADDING_TOP + plot_height

    parts = [
        (
            f'<svg viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" role="img" '
            'aria-label="Monthly income and spending bars" xmlns="http://www.w3.org/2000/svg">'
        ),
        f'<line x1="{CHART_PADDING_LEFT}" y1="{baseline}" x2="{CHART_WIDTH}" y2="{baseline}" stroke="#888"/>',
    ]
    for index, item in enumerate(summaries):
        group_left = CHART_PADDING_LEFT + index * group_width
        parts.append(
            _bar(
                group_left + group_width * 0.1,
                baseline,
                item.income,
                scale,
                bar_width,
                INCOME_COLOR,
                f"Income {item.income}",
            )
        )
        parts.append(
            _bar(
                group_left + group_width * 0.55,
                baseline,
                item.spending,
                scale,
                bar_width,
                SPENDING_COLOR,
                f"Spending {item.spending}",
            )
        )
        parts.append(
            f'<text x="{group_left + group_width / 2:.1f}" y="{CHART_HEIGHT - 12}" '
            f'text-anchor="middle" font-size="12">{escape(item.month)}</text>'
        )
    parts.append(
        f'<text x="8" y="{CHART_PADDING_TOP + 10}" font-size="12">{escape(_format_money(peak))}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _bar(
    left: float,
    baseline: float,
    value: Decimal,
    scale: float,
    width: float,
    color: str,
    label: str,
) -> str:
    """Draw one bar with a tooltip.

    Args:
        left: X position of the bar.
        baseline: Y position of the bar's bottom edge.
        value: Amount to draw.
        scale: Pixels per currency unit.
        width: Bar width in pixels.
        color: Fill color.
        label: Tooltip text.

    Returns:
        An SVG rect element.
    """
    height = float(value) * scale
    return (
        f'<rect x="{left:.1f}" y="{baseline - height:.1f}" width="{width:.1f}" '
        f'height="{height:.1f}" fill="{color}"><title>{escape(label)}</title></rect>'
    )


def category_bars_html(totals: Sequence[tuple[str, Decimal]]) -> str:
    """Draw spending by category as horizontal bars.

    Args:
        totals: (category, total) pairs, largest first.

    Returns:
        An HTML list, or a short message if there is no spending.
    """
    if not totals:
        return "<p>No spending recorded for this month.</p>"

    largest = totals[0][1]
    rows = []
    for category, total in totals:
        percent = float(total / largest * 100) if largest > 0 else 0.0
        rows.append(
            '<li class="category">'
            f'<span class="label">{escape(_category_label(category))}</span>'
            f'<span class="track"><span class="fill" style="width: {percent:.1f}%"></span></span>'
            f'<span class="amount">{escape(_format_money(total))}</span>'
            "</li>"
        )
    return f'<ul class="categories">{"".join(rows)}</ul>'


def _category_label(category: str) -> str:
    """Turn FOOD_AND_DRINK into Food And Drink.

    Args:
        category: Raw category name.

    Returns:
        A title-cased label.
    """
    return category.replace("_", " ").title()


def _format_money(value: Decimal) -> str:
    """Format money as $1,234.50, or -$1,234.50 when negative.

    Args:
        value: Amount to format.

    Returns:
        The formatted string.
    """
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


# --- Data and routing ----------------------------------------------------------


@dataclass(frozen=True)
class Response:
    """An HTTP response ready to send.

    Attributes:
        status: HTTP status code.
        content_type: Content-Type header value.
        body: Response body text.
    """

    status: int
    content_type: str
    body: str


class DashboardData:
    """Load the stored ledger and status, and cache them for a short time."""

    def __init__(
        self, store: Store, cache_seconds: int, clock: Any = time.monotonic
    ) -> None:
        """Create the cache.

        Args:
            store: Store that holds the ledger CSV and status file.
            cache_seconds: How long to keep loaded data before reloading.
            clock: Monotonic clock function. Tests pass a fake.
        """
        self._store = store
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._loaded_at: float | None = None
        self._rows: list[dict[str, str]] = []
        self._status: dict[str, Any] | None = None

    def rows(self) -> list[dict[str, str]]:
        """Return ledger rows, reloading if the cache has expired.

        Returns:
            Ledger rows, possibly empty.
        """
        self._reload_if_stale()
        return self._rows

    def status(self) -> dict[str, Any] | None:
        """Return the last sync status, reloading if the cache has expired.

        Returns:
            The status document, or None if none has been recorded.
        """
        self._reload_if_stale()
        return self._status

    def _reload_if_stale(self) -> None:
        """Reload both documents when the cache has expired."""
        now = self._clock()
        if self._loaded_at is not None and now - self._loaded_at < self._cache_seconds:
            return
        self._rows = list(
            csv_to_ledger(self._store.read_text(PROCESSED_KEY) or "").values()
        )
        status_text = self._store.read_text(STATUS_KEY)
        self._status = json.loads(status_text) if status_text else None
        self._loaded_at = now


def route(path: str, query: dict[str, list[str]], data: DashboardData) -> Response:
    """Map a request to a response.

    Args:
        path: URL path without the query string.
        query: Parsed query parameters.
        data: Data cache to read from.

    Returns:
        The response to send.
    """
    try:
        if path == "/":
            return _dashboard_page(query, data)
        if path == "/api/summary":
            return _json(
                200, [_summary_json(item) for item in monthly_summaries(data.rows())]
            )
        if path == "/api/categories":
            month = _month_param(query)
            totals = category_totals(data.rows(), month) if month else []
            return _json(
                200,
                {
                    "month": month,
                    "categories": [
                        {"category": name, "total": str(total)}
                        for name, total in totals
                    ],
                },
            )
        if path == "/api/status":
            return _json(200, data.status() or {"state": "not_run"})
    except ValueError as err:
        return _json(400, {"error": str(err)})
    except Exception:
        LOGGER.exception("Unhandled error while serving %s", path)
        return _json(500, {"error": "internal error"})
    return _json(404, {"error": "not found"})


def _dashboard_page(query: dict[str, list[str]], data: DashboardData) -> Response:
    """Build the dashboard page for the requested month.

    Args:
        query: Parsed query parameters, may include month.
        data: Data cache.

    Returns:
        An HTML response.

    Raises:
        ValueError: If the month parameter is malformed.
    """
    rows = data.rows()
    summaries = monthly_summaries(rows)
    month = _month_param(query) or (summaries[-1].month if summaries else None)
    selected = next((item for item in summaries if item.month == month), None)
    totals = category_totals(rows, month) if month else []
    heading_suffix = escape(f" ({month})") if month else ""

    body = "".join(
        [
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Personal Finance Dashboard (Sandbox)</title>",
            f"<style>{PAGE_STYLE}</style></head><body>",
            "<header><h1>Personal Finance Dashboard</h1>",
            '<p class="notice">Sandbox data only. Do not connect real accounts.</p></header>',
            _month_form(summaries, month),
            _cards(selected),
            "<h2>Income and spending by month</h2>",
            monthly_chart_svg(summaries),
            f"<h2>Spending by category{heading_suffix}</h2>",
            category_bars_html(totals),
            _status_line(data.status()),
            "</body></html>",
        ]
    )
    return Response(200, "text/html; charset=utf-8", body)


def _month_form(summaries: Sequence[MonthSummary], selected_month: str | None) -> str:
    """Build the month selector. It uses a submit button, so no script is needed.

    Args:
        summaries: Summaries that supply the options.
        selected_month: Month to mark as selected.

    Returns:
        An HTML form.
    """
    options = "".join(
        f'<option value="{escape(item.month)}"'
        f"{' selected' if item.month == selected_month else ''}>{escape(item.month)}</option>"
        for item in summaries
    )
    return (
        '<form method="get" action="/"><label>Month '
        f'<select name="month">{options}</select></label> '
        '<button type="submit">Show</button></form>'
    )


def _cards(selected: MonthSummary | None) -> str:
    """Build the income, spending, and savings cards.

    Args:
        selected: Summary for the chosen month, or None.

    Returns:
        A row of cards as HTML.
    """
    income = selected.income if selected else Decimal(0)
    spending = selected.spending if selected else Decimal(0)
    savings = selected.savings if selected else Decimal(0)
    items = "".join(
        f'<div class="card"><div>{label}</div>'
        f'<div class="value">{_format_money(value)}</div></div>'
        for label, value in (
            ("Income", income),
            ("Spending", spending),
            ("Savings", savings),
        )
    )
    return f'<section class="cards">{items}</section>'


def _status_line(status: dict[str, Any] | None) -> str:
    """Describe the last sync in one line.

    Args:
        status: Status document, or None.

    Returns:
        A paragraph of HTML.
    """
    if status is None:
        return '<p class="status">No sync status has been recorded.</p>'
    state = escape(str(status.get("state", "unknown")))
    last_run = escape(str(status.get("last_run_utc", "unknown")))
    detail = ""
    if status.get("state") == "failed":
        detail = f" Error: {escape(str(status.get('error', 'unknown')))}"
    return f'<p class="status">Last sync {last_run} (state: {state}).{detail}</p>'


def _month_param(query: dict[str, list[str]]) -> str | None:
    """Read and validate the month parameter.

    Args:
        query: Parsed query parameters.

    Returns:
        The month as YYYY-MM, or None if absent.

    Raises:
        ValueError: If the month is not a real YYYY-MM month.
    """
    values = query.get("month")
    if not values:
        return None
    month = values[0]
    if not MONTH_PATTERN.fullmatch(month):
        raise ValueError("month must look like YYYY-MM")
    try:
        date.fromisoformat(f"{month}-01")
    except ValueError as err:
        raise ValueError("month must be a real calendar month") from err
    return month


def _summary_json(item: MonthSummary) -> dict[str, str]:
    """Convert a summary to JSON-safe strings.

    Args:
        item: Summary to convert.

    Returns:
        A dictionary with string money values.
    """
    return {
        "month": item.month,
        "income": str(item.income),
        "spending": str(item.spending),
        "savings": str(item.savings),
    }


def _json(status: int, payload: Any) -> Response:
    """Build a JSON response.

    Args:
        status: HTTP status code.
        payload: Value to serialize.

    Returns:
        A JSON response.
    """
    return Response(status, "application/json", json.dumps(payload))


# --- HTTP ----------------------------------------------------------------------


class DashboardHandler(BaseHTTPRequestHandler):
    """Request handler. Only GET is supported."""

    data: DashboardData

    def do_GET(self) -> None:
        """Handle a GET request."""
        parts = urlsplit(self.path)
        self._send(route(parts.path, parse_qs(parts.query), self.data))

    def do_POST(self) -> None:
        """Reject POST requests."""
        self._send(_json(405, {"error": "method not allowed"}))

    do_PUT = do_POST
    do_DELETE = do_POST

    def _send(self, response: Response) -> None:
        """Write a response with security headers.

        Args:
            response: Response to send.
        """
        encoded = response.body.encode("utf-8")
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        """Send access logs to the logging module instead of stderr.

        Args:
            format: Format string from http.server.
            *args: Values for the format string.
        """
        LOGGER.info("%s - %s", self.address_string(), format % args)


def make_server(host: str, port: int, data: DashboardData) -> HTTPServer:
    """Create an HTTP server bound to an address.

    Args:
        host: Address to bind.
        port: Port to bind.
        data: Data cache the handler reads from.

    Returns:
        A configured server that is not yet serving.
    """
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"data": data})
    return HTTPServer((host, port), handler)


def main() -> int:
    """Start the dashboard.

    Returns:
        0 when the server stops cleanly, 1 on bad configuration.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()
    store = open_store(settings)
    host = os.environ.get("DASHBOARD_HOST", DEFAULT_DASHBOARD_HOST)
    try:
        port = int(os.environ.get("DASHBOARD_PORT", str(DEFAULT_DASHBOARD_PORT)))
        cache_seconds = int(os.environ.get("CACHE_SECONDS", str(DEFAULT_CACHE_SECONDS)))
    except ValueError:
        LOGGER.error("DASHBOARD_PORT and CACHE_SECONDS must be integers")
        return 1
    if cache_seconds < 0:
        LOGGER.error("CACHE_SECONDS must not be negative")
        return 1

    server = make_server(host, port, DashboardData(store, cache_seconds))
    LOGGER.info("Serving dashboard on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
