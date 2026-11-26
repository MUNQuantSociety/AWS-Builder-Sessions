"""Plaid sandbox to storage: fetch transactions and save them for the dashboard.

The instructor runs this once to create the static data. client.py reads the
result. Settings come from environment variables:

    BUCKET_NAME        S3 bucket name. Leave empty to write to LOCAL_STORE_DIR.
    STORE_PREFIX       Per-participant prefix, such as participants/alice/
    LOCAL_STORE_DIR    Local folder used when BUCKET_NAME is empty
    PLAID_CLIENT_ID    Plaid sandbox client ID
    PLAID_SECRET       Plaid sandbox secret
    PLAID_ACCESS_TOKEN Access token for one sandbox item

Usage:
    python server.py                      # live sandbox sync
    python server.py --from-sample FILE   # replay saved sync pages, offline

The exit code is 0 on success and 1 on failure. Failures are also written to
status/latest.json so the dashboard can show them.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

LOGGER = logging.getLogger("server")

PLAID_BASE_URL = "https://sandbox.plaid.com"
PLAID_SYNC_PATH = "/transactions/sync"
PLAID_PAGE_SIZE = 100
PLAID_REQUEST_TIMEOUT_SECONDS = 20
PLAID_MAX_PAGES_PER_RUN = 20
PLAID_DELAY_BETWEEN_PAGES_SECONDS = 1.0

DEFAULT_STORE_PREFIX = "participants/demo/"
DEFAULT_LOCAL_STORE_DIR = "./local_store"
STORE_PREFIX_PATTERN = re.compile(r"participants/[a-z0-9][a-z0-9-]{0,39}/")

CURSOR_KEY = "state/cursor.json"
PROCESSED_KEY = "processed/transactions.csv"
STATUS_KEY = "status/latest.json"

CSV_COLUMNS = ("transaction_id", "date", "name", "amount", "category", "pending")
UNCATEGORIZED = "UNCATEGORIZED"
CENT = Decimal("0.01")
# Spreadsheet apps run a cell as a formula when it starts with one of these.
# Text fields that start with one get an apostrophe, and the apostrophe itself
# is escaped too, so the CSV round trip stays exact.
ESCAPE_TRIGGERS = ("=", "+", "-", "@", "'")

Opener = Callable[..., Any]
Fetcher = Callable[["Settings", "str | None"], "list[dict[str, Any]]"]


class PlaidError(RuntimeError):
    """Raised when Plaid cannot be reached or returns an error."""


@dataclass(frozen=True)
class Settings:
    """Settings for the sync and the storage location.

    Attributes:
        bucket_name: S3 bucket name. None means use the local store.
        store_prefix: Per-participant prefix, such as participants/alice/.
        local_store_dir: Root folder for the local store.
        plaid_client_id: Plaid sandbox client ID.
        plaid_secret: Plaid sandbox secret.
        plaid_access_token: Access token for one sandbox item.
    """

    bucket_name: str | None
    store_prefix: str
    local_store_dir: str
    plaid_client_id: str | None
    plaid_secret: str | None
    plaid_access_token: str | None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from environment variables.

    Args:
        environ: Mapping to read from. Defaults to os.environ.

    Returns:
        A validated Settings object.

    Raises:
        ValueError: If STORE_PREFIX does not look like participants/<name>/.
    """
    env = os.environ if environ is None else environ
    prefix = env.get("STORE_PREFIX", DEFAULT_STORE_PREFIX)
    if not STORE_PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError(
            f"STORE_PREFIX must look like participants/<name>/, got {prefix!r}"
        )
    return Settings(
        bucket_name=env.get("BUCKET_NAME") or None,
        store_prefix=prefix,
        local_store_dir=env.get("LOCAL_STORE_DIR", DEFAULT_LOCAL_STORE_DIR),
        plaid_client_id=env.get("PLAID_CLIENT_ID") or None,
        plaid_secret=env.get("PLAID_SECRET") or None,
        plaid_access_token=env.get("PLAID_ACCESS_TOKEN") or None,
    )


# --- Storage -----------------------------------------------------------------


class Store(Protocol):
    """Text storage used by both programs."""

    def read_text(self, key: str) -> str | None:
        """Return the text under key, or None if it does not exist."""

    def write_text(self, key: str, text: str) -> None:
        """Store text under key, replacing any existing value."""


class LocalStore:
    """Store files under a root folder on local disk."""

    def __init__(self, root: Path) -> None:
        """Create a store rooted at a folder.

        Args:
            root: Folder that holds every key. Created on first write.
        """
        self._root = root.resolve()

    def _path_for(self, key: str) -> Path:
        """Resolve a key to a path that stays inside the root.

        Args:
            key: Relative key, such as processed/transactions.csv.

        Returns:
            The resolved path.

        Raises:
            ValueError: If the key would point outside the root.
        """
        path = (self._root / key).resolve()
        if path != self._root and self._root not in path.parents:
            raise ValueError(f"key escapes the store root: {key!r}")
        return path

    def read_text(self, key: str) -> str | None:
        """Read a file, or return None if it is missing.

        Args:
            key: Relative key to read.

        Returns:
            The file contents, or None.
        """
        path = self._path_for(key)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def write_text(self, key: str, text: str) -> None:
        """Write a file, creating folders as needed.

        Args:
            key: Relative key to write.
            text: Contents to store.
        """
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class S3Store:
    """Store objects in one S3 bucket under a fixed prefix."""

    def __init__(self, bucket: str, prefix: str, client: Any | None = None) -> None:
        """Create a store for one bucket and prefix.

        Args:
            bucket: S3 bucket name.
            prefix: Key prefix, such as participants/alice/.
            client: Optional boto3 S3 client. Created from the default
                credential chain when omitted.
        """
        self._bucket = bucket
        self._prefix = prefix
        if client is None:
            import boto3

            client = boto3.client("s3")
        self._client = client

    def _key_for(self, key: str) -> str:
        """Join the prefix and a relative key.

        Args:
            key: Relative key.

        Returns:
            The full S3 key.

        Raises:
            ValueError: If the key is absolute or contains "..".
        """
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"invalid key: {key!r}")
        return f"{self._prefix}{key}"

    def read_text(self, key: str) -> str | None:
        """Read an object, or return None if it is missing.

        Args:
            key: Relative key to read.

        Returns:
            The object contents, or None.

        Raises:
            botocore.exceptions.ClientError: For errors other than a missing key.
        """
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=self._key_for(key)
            )
        except ClientError as err:
            if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read().decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        """Write an object.

        Args:
            key: Relative key to write.
            text: Contents to store.
        """
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key_for(key),
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )


def open_store(settings: Settings) -> Store:
    """Choose the storage backend for the configured prefix.

    Args:
        settings: Loaded settings.

    Returns:
        An S3Store when BUCKET_NAME is set, otherwise a LocalStore.
    """
    if settings.bucket_name:
        return S3Store(settings.bucket_name, settings.store_prefix)
    return LocalStore(Path(settings.local_store_dir) / settings.store_prefix)


# --- Plaid sandbox -----------------------------------------------------------


def fetch_sync_pages(
    settings: Settings,
    cursor: str | None,
    *,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch transaction updates from the Plaid sandbox, one page at a time.

    Args:
        settings: Settings with the Plaid credentials.
        cursor: Cursor from the previous run, or None for a full sync.
        opener: Function that sends requests. Tests pass a fake.
        sleep: Function that pauses between pages. Tests pass a no-op.

    Returns:
        The pages in order. The last page holds the next cursor. If the page
        limit is reached, the pages are partial and the next run resumes.

    Raises:
        PlaidError: If credentials are missing or Plaid returns an error.
    """
    missing = [
        name
        for name, value in (
            ("PLAID_CLIENT_ID", settings.plaid_client_id),
            ("PLAID_SECRET", settings.plaid_secret),
            ("PLAID_ACCESS_TOKEN", settings.plaid_access_token),
        )
        if not value
    ]
    if missing:
        raise PlaidError(f"missing environment variables: {', '.join(missing)}")

    pages: list[dict[str, Any]] = []
    current_cursor = cursor
    for page_number in range(PLAID_MAX_PAGES_PER_RUN):
        page = _post_sync(settings, current_cursor, opener)
        pages.append(page)
        current_cursor = page["next_cursor"]
        if not page.get("has_more", False):
            return pages
        if page_number < PLAID_MAX_PAGES_PER_RUN - 1:
            sleep(PLAID_DELAY_BETWEEN_PAGES_SECONDS)

    LOGGER.warning(
        "Stopped after %d pages; the next run will resume", PLAID_MAX_PAGES_PER_RUN
    )
    return pages


def _post_sync(
    settings: Settings, cursor: str | None, opener: Opener
) -> dict[str, Any]:
    """Send one sync request and parse the response.

    Args:
        settings: Settings with credentials.
        cursor: Cursor to resume from, or None.
        opener: Function that sends the request.

    Returns:
        The parsed response body.

    Raises:
        PlaidError: On HTTP errors, network errors, or malformed responses.
    """
    body: dict[str, Any] = {
        "client_id": settings.plaid_client_id,
        "secret": settings.plaid_secret,
        "access_token": settings.plaid_access_token,
        "count": PLAID_PAGE_SIZE,
    }
    if cursor:
        body["cursor"] = cursor

    request = urllib.request.Request(
        PLAID_BASE_URL + PLAID_SYNC_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=PLAID_REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as err:
        raise PlaidError(_describe_http_error(err)) from None
    except urllib.error.URLError as err:
        raise PlaidError(f"could not reach Plaid: {err.reason}") from None

    try:
        page = json.loads(raw)
    except json.JSONDecodeError as err:
        raise PlaidError("Plaid returned a response that is not valid JSON") from err
    if "next_cursor" not in page:
        raise PlaidError("Plaid response is missing next_cursor")
    return page


def _describe_http_error(err: urllib.error.HTTPError) -> str:
    """Describe an HTTP error without echoing request or response secrets.

    Args:
        err: The HTTP error raised by urllib.

    Returns:
        A message with the status code and Plaid's error code.
    """
    error_code = "unknown"
    try:
        payload = json.loads(err.read())
        error_code = str(payload.get("error_code", "unknown"))
    except (json.JSONDecodeError, OSError, AttributeError):
        pass
    return f"Plaid returned HTTP {err.code} ({error_code})"


# --- Ledger --------------------------------------------------------------------


def apply_sync_pages(
    ledger: dict[str, dict[str, str]], pages: Iterable[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    """Apply added, modified, and removed transactions from each page, in order.

    Args:
        ledger: Current ledger keyed by transaction_id. Not modified.
        pages: Sync pages in the order Plaid returned them.

    Returns:
        A new ledger with every page applied.
    """
    updated = dict(ledger)
    for page in pages:
        for transaction in [*page.get("added", []), *page.get("modified", [])]:
            row = _to_row(transaction)
            updated[row["transaction_id"]] = row
        for removed in page.get("removed", []):
            updated.pop(removed["transaction_id"], None)
    return updated


def _to_row(transaction: dict[str, Any]) -> dict[str, str]:
    """Convert one Plaid transaction to a flat ledger row.

    Args:
        transaction: Transaction object from a sync page.

    Returns:
        A row with the CSV_COLUMNS keys, all strings.
    """
    category_info = transaction.get("personal_finance_category") or {}
    return {
        "transaction_id": str(transaction["transaction_id"]),
        "date": str(transaction["date"]),
        "name": str(transaction.get("name") or ""),
        "amount": _format_amount(transaction["amount"]),
        "category": str(category_info.get("primary") or UNCATEGORIZED),
        "pending": "true" if transaction.get("pending") else "false",
    }


def _format_amount(raw: Any) -> str:
    """Format an amount with exactly two decimal places.

    Args:
        raw: Number or numeric string from Plaid.

    Returns:
        A string such as -2500.00.

    Raises:
        ValueError: If the amount is not numeric.
    """
    try:
        return f"{Decimal(str(raw)).quantize(CENT, rounding=ROUND_HALF_UP):f}"
    except InvalidOperation as err:
        raise ValueError(f"amount is not numeric: {raw!r}") from err


def ledger_to_csv(ledger: dict[str, dict[str, str]]) -> str:
    """Serialize the ledger to CSV, sorted by date and then transaction_id.

    Args:
        ledger: Ledger to serialize.

    Returns:
        CSV text with a header row.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in sorted(
        ledger.values(), key=lambda item: (item["date"], item["transaction_id"])
    ):
        writer.writerow(_escape_row(row))
    return buffer.getvalue()


def csv_to_ledger(text: str) -> dict[str, dict[str, str]]:
    """Parse CSV written by ledger_to_csv.

    Args:
        text: CSV text with the CSV_COLUMNS header. Empty text is allowed.

    Returns:
        A ledger keyed by transaction_id.

    Raises:
        ValueError: If the header does not match CSV_COLUMNS.
    """
    if not text.strip():
        return {}
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise ValueError("ledger CSV header does not match the expected columns")
    return {row["transaction_id"]: _unescape_row(row) for row in reader}


def _escape_row(row: dict[str, str]) -> dict[str, str]:
    """Protect free-text fields from spreadsheet formula injection.

    Args:
        row: Row to escape. The amount column is never escaped, because
            negative amounts are valid.

    Returns:
        A copy of the row with dangerous text prefixed by an apostrophe.
    """
    escaped = dict(row)
    for column in ("name", "category"):
        if escaped[column].startswith(ESCAPE_TRIGGERS):
            escaped[column] = "'" + escaped[column]
    return escaped


def _unescape_row(row: dict[str, str]) -> dict[str, str]:
    """Remove the apostrophe that _escape_row added.

    Args:
        row: Row read from CSV.

    Returns:
        A copy of the row with the escape removed from text fields.
    """
    restored = dict(row)
    for column in ("name", "category"):
        value = restored[column]
        if value.startswith("'") and value[1:].startswith(ESCAPE_TRIGGERS):
            restored[column] = value[1:]
    return restored


# --- Sync run --------------------------------------------------------------------


def load_cursor(store: Store) -> str | None:
    """Read the saved sync cursor.

    Args:
        store: Store that holds the cursor.

    Returns:
        The cursor, or None on the first run.
    """
    text = store.read_text(CURSOR_KEY)
    if text is None:
        return None
    cursor = json.loads(text).get("cursor")
    return cursor if isinstance(cursor, str) and cursor else None


def run_sync(
    settings: Settings, store: Store, fetch: Fetcher, today: str
) -> dict[str, Any]:
    """Fetch new transactions, merge them, and save everything.

    Args:
        settings: Settings passed through to the fetcher.
        store: Store to read from and write to.
        fetch: Function that returns sync pages for a cursor.
        today: Run date as YYYY-MM-DD, used for the raw folder name.

    Returns:
        The status document that was written.

    Raises:
        PlaidError: If the fetch fails. The failure is recorded first.
    """
    try:
        pages = fetch(settings, load_cursor(store))
    except PlaidError as err:
        store.write_text(
            STATUS_KEY, json.dumps(_status("failed", error=str(err)), indent=2)
        )
        raise

    for index, page in enumerate(pages):
        store.write_text(
            f"raw/{today}/sync-{index:03d}.json", json.dumps(page, indent=2)
        )

    ledger = apply_sync_pages(
        csv_to_ledger(store.read_text(PROCESSED_KEY) or ""), pages
    )
    store.write_text(PROCESSED_KEY, ledger_to_csv(ledger))
    if pages:
        store.write_text(CURSOR_KEY, json.dumps({"cursor": pages[-1]["next_cursor"]}))

    status = _status(
        "ok",
        rows=len(ledger),
        pages_fetched=len(pages),
        latest_date=max((row["date"] for row in ledger.values()), default=None),
    )
    store.write_text(STATUS_KEY, json.dumps(status, indent=2))
    LOGGER.info("Stored %d transactions from %d pages", len(ledger), len(pages))
    return status


def _status(state: str, **details: Any) -> dict[str, Any]:
    """Build a status document.

    Args:
        state: ok or failed.
        **details: Extra fields to include.

    Returns:
        The state, the UTC run time, and the details.
    """
    return {
        "state": state,
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        **details,
    }


def _sample_fetch(sample_path: Path) -> Fetcher:
    """Build a fetcher that replays saved sync pages.

    Args:
        sample_path: JSON file holding a list of sync pages.

    Returns:
        A fetcher with the same signature as fetch_sync_pages.
    """

    def fetch(_settings: Settings, _cursor: str | None) -> list[dict[str, Any]]:
        return json.loads(sample_path.read_text(encoding="utf-8"))

    return fetch


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments to parse. Defaults to sys.argv.

    Returns:
        0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(
        description="Fetch Plaid sandbox transactions into storage."
    )
    parser.add_argument(
        "--from-sample",
        type=Path,
        help="Replay sync pages from a JSON file instead of calling Plaid.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()
    store = open_store(settings)
    fetch: Fetcher = (
        _sample_fetch(args.from_sample) if args.from_sample else fetch_sync_pages
    )
    today = datetime.now(timezone.utc).date().isoformat()

    try:
        run_sync(settings, store, fetch, today)
    except PlaidError as err:
        LOGGER.error("Sync failed: %s", err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
