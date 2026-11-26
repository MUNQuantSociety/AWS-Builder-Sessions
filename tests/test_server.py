"""Tests for server.py: settings, storage, the Plaid client, the ledger, and the sync run."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

import server
from server import (
    CURSOR_KEY,
    PLAID_MAX_PAGES_PER_RUN,
    PROCESSED_KEY,
    STATUS_KEY,
    LocalStore,
    PlaidError,
    S3Store,
    Settings,
    apply_sync_pages,
    csv_to_ledger,
    fetch_sync_pages,
    ledger_to_csv,
    load_settings,
    run_sync,
)

SAMPLE_PAGES = (
    Path(__file__).resolve().parent.parent / "sample_data" / "sync_pages.json"
)
SECRET = "sandbox-secret-value"
ACCESS_TOKEN = "access-sandbox-token-value"


def _settings(**overrides: Any) -> Settings:
    """Build settings with sandbox credentials for tests."""
    values: dict[str, Any] = {
        "bucket_name": None,
        "store_prefix": "participants/demo/",
        "local_store_dir": "./unused",
        "plaid_client_id": "client-123",
        "plaid_secret": SECRET,
        "plaid_access_token": ACCESS_TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


def _txn(transaction_id: str, amount: float = 10.0, **extra: object) -> dict[str, Any]:
    """Build a minimal Plaid-style transaction."""
    transaction: dict[str, Any] = {
        "transaction_id": transaction_id,
        "date": "2026-10-03",
        "name": "Test Shop",
        "amount": amount,
        "pending": False,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
    }
    transaction.update(extra)
    return transaction


def _page(next_cursor: str, has_more: bool) -> dict[str, Any]:
    """Build a sync page with no changes."""
    return {
        "added": [],
        "modified": [],
        "removed": [],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


class FakeOpener:
    """Records request bodies and returns queued responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> io.BytesIO:
        self.bodies.append(json.loads(request.data))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode("utf-8"))


# --- Settings ---------------------------------------------------------------------


def test_defaults_when_nothing_is_set() -> None:
    settings = load_settings({})

    assert settings.bucket_name is None
    assert settings.store_prefix == "participants/demo/"
    assert settings.plaid_access_token is None


def test_values_are_read_from_the_environment() -> None:
    settings = load_settings(
        {
            "BUCKET_NAME": "shared-bucket",
            "STORE_PREFIX": "participants/alice/",
            "PLAID_CLIENT_ID": "client",
            "PLAID_SECRET": "secret",
            "PLAID_ACCESS_TOKEN": "token",
        }
    )

    assert settings.bucket_name == "shared-bucket"
    assert settings.store_prefix == "participants/alice/"
    assert settings.plaid_access_token == "token"


@pytest.mark.parametrize(
    "prefix",
    [
        "participants/alice",
        "participants/Alice/",
        "../participants/alice/",
        "participants/alice/../bob/",
        "other/alice/",
    ],
)
def test_invalid_store_prefixes_are_rejected(prefix: str) -> None:
    with pytest.raises(ValueError, match="STORE_PREFIX"):
        load_settings({"STORE_PREFIX": prefix})


# --- Storage ----------------------------------------------------------------------


def test_local_store_round_trip_and_missing_key(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)

    store.write_text("processed/transactions.csv", "a,b\n")

    assert store.read_text("processed/transactions.csv") == "a,b\n"
    assert store.read_text("missing.json") is None


@pytest.mark.parametrize("key", ["../escape.txt", "a/../../escape.txt", "/etc/passwd"])
def test_local_store_refuses_keys_outside_its_root(tmp_path: Path, key: str) -> None:
    store = LocalStore(tmp_path / "root")

    with pytest.raises(ValueError, match="escapes"):
        store.write_text(key, "nope")

    assert not (tmp_path / "escape.txt").exists()


class FakeS3Client:
    """In-memory stand-in for the boto3 S3 client."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs: Any) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs["Body"]

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[key])}


def test_s3_store_applies_the_prefix_to_every_key() -> None:
    client = FakeS3Client()
    store = S3Store("shared-bucket", "participants/alice/", client=client)

    store.write_text("status/latest.json", "{}")

    assert ("shared-bucket", "participants/alice/status/latest.json") in client.objects


def test_s3_store_round_trip_and_missing_key() -> None:
    pytest.importorskip("botocore")
    store = S3Store("shared-bucket", "participants/alice/", client=FakeS3Client())

    store.write_text("state/cursor.json", '{"cursor": "c1"}')

    assert store.read_text("state/cursor.json") == '{"cursor": "c1"}'
    assert store.read_text("missing.json") is None


def test_s3_store_rejects_parent_directory_keys() -> None:
    store = S3Store("shared-bucket", "participants/alice/", client=FakeS3Client())

    with pytest.raises(ValueError, match="invalid key"):
        store.write_text("../bob/status.json", "{}")


# --- Plaid client -----------------------------------------------------------------


def test_pages_are_fetched_until_has_more_is_false() -> None:
    opener = FakeOpener([_page("c1", True), _page("c2", False)])
    sleeps: list[float] = []

    pages = fetch_sync_pages(_settings(), None, opener=opener, sleep=sleeps.append)

    assert [page["next_cursor"] for page in pages] == ["c1", "c2"]
    assert len(sleeps) == 1


def test_second_request_sends_the_cursor_from_the_first_page() -> None:
    opener = FakeOpener([_page("c1", True), _page("c2", False)])

    fetch_sync_pages(_settings(), None, opener=opener, sleep=lambda _: None)

    assert "cursor" not in opener.bodies[0]
    assert opener.bodies[1]["cursor"] == "c1"


def test_saved_cursor_is_sent_on_the_first_request() -> None:
    opener = FakeOpener([_page("c9", False)])

    fetch_sync_pages(_settings(), "saved-cursor", opener=opener, sleep=lambda _: None)

    assert opener.bodies[0]["cursor"] == "saved-cursor"


def test_request_carries_credentials_in_the_body() -> None:
    opener = FakeOpener([_page("c1", False)])

    fetch_sync_pages(_settings(), None, opener=opener, sleep=lambda _: None)

    assert opener.bodies[0]["client_id"] == "client-123"
    assert opener.bodies[0]["secret"] == SECRET


def test_page_limit_stops_the_run_and_keeps_the_cursor() -> None:
    opener = FakeOpener([_page(f"c{i}", True) for i in range(PLAID_MAX_PAGES_PER_RUN)])

    pages = fetch_sync_pages(_settings(), None, opener=opener, sleep=lambda _: None)

    assert len(pages) == PLAID_MAX_PAGES_PER_RUN
    assert pages[-1]["next_cursor"] == f"c{PLAID_MAX_PAGES_PER_RUN - 1}"


def test_missing_credentials_name_the_variables_but_not_their_values() -> None:
    settings = _settings(plaid_secret=None, plaid_access_token=None)

    with pytest.raises(PlaidError, match="PLAID_SECRET, PLAID_ACCESS_TOKEN") as info:
        fetch_sync_pages(settings, None, opener=FakeOpener([]), sleep=lambda _: None)

    assert SECRET not in str(info.value)


def test_http_error_reports_status_and_error_code_only() -> None:
    error_body = io.BytesIO(
        json.dumps({"error_code": "INVALID_ACCESS_TOKEN", "secret": SECRET}).encode()
    )
    http_error = urllib.error.HTTPError(
        "https://sandbox.plaid.com/transactions/sync",
        400,
        "Bad Request",
        {},
        error_body,
    )

    with pytest.raises(PlaidError) as info:
        fetch_sync_pages(
            _settings(), None, opener=FakeOpener([http_error]), sleep=lambda _: None
        )

    message = str(info.value)
    assert "HTTP 400" in message
    assert "INVALID_ACCESS_TOKEN" in message
    assert SECRET not in message
    assert ACCESS_TOKEN not in message


def test_network_failure_becomes_a_plaid_error() -> None:
    network_error = urllib.error.URLError("name resolution failed")

    with pytest.raises(PlaidError, match="could not reach Plaid"):
        fetch_sync_pages(
            _settings(), None, opener=FakeOpener([network_error]), sleep=lambda _: None
        )


def test_invalid_json_becomes_a_plaid_error() -> None:
    def bad_body_opener(request: Any, timeout: float) -> io.BytesIO:
        return io.BytesIO(b"<html>not json</html>")

    with pytest.raises(PlaidError, match="not valid JSON"):
        fetch_sync_pages(
            _settings(), None, opener=bad_body_opener, sleep=lambda _: None
        )


def test_response_without_next_cursor_is_rejected() -> None:
    with pytest.raises(PlaidError, match="next_cursor"):
        fetch_sync_pages(
            _settings(),
            None,
            opener=FakeOpener([{"has_more": False}]),
            sleep=lambda _: None,
        )


# --- Ledger -----------------------------------------------------------------------


def test_added_transaction_is_normalized_to_a_row() -> None:
    ledger = apply_sync_pages(
        {}, [{"added": [_txn("t1", 12.5)], "modified": [], "removed": []}]
    )

    assert ledger == {
        "t1": {
            "transaction_id": "t1",
            "date": "2026-10-03",
            "name": "Test Shop",
            "amount": "12.50",
            "category": "FOOD_AND_DRINK",
            "pending": "false",
        }
    }


def test_missing_category_defaults_to_uncategorized() -> None:
    transaction = _txn("t1")
    del transaction["personal_finance_category"]

    ledger = apply_sync_pages(
        {}, [{"added": [transaction], "modified": [], "removed": []}]
    )

    assert ledger["t1"]["category"] == "UNCATEGORIZED"


def test_modified_transaction_replaces_the_existing_row() -> None:
    page_one = {"added": [_txn("t1", 10.0)], "modified": [], "removed": []}
    page_two = {"added": [], "modified": [_txn("t1", 14.25)], "removed": []}

    ledger = apply_sync_pages({}, [page_one, page_two])

    assert ledger["t1"]["amount"] == "14.25"
    assert len(ledger) == 1


def test_removed_transaction_is_deleted_and_unknown_ids_are_ignored() -> None:
    ledger = apply_sync_pages(
        {}, [{"added": [_txn("t1"), _txn("t2")], "modified": [], "removed": []}]
    )

    updated = apply_sync_pages(
        ledger,
        [
            {
                "added": [],
                "modified": [],
                "removed": [{"transaction_id": "t1"}, {"transaction_id": "missing"}],
            }
        ],
    )

    assert set(updated) == {"t2"}


def test_apply_does_not_change_the_input_ledger() -> None:
    original = apply_sync_pages(
        {}, [{"added": [_txn("t1")], "modified": [], "removed": []}]
    )
    snapshot = dict(original)

    apply_sync_pages(
        original, [{"added": [], "modified": [], "removed": [{"transaction_id": "t1"}]}]
    )

    assert original == snapshot


def test_csv_round_trip_keeps_every_row() -> None:
    ledger = apply_sync_pages(
        {},
        [
            {
                "added": [_txn("t1", 5.0), _txn("t2", -40.0, date="2026-09-01")],
                "modified": [],
                "removed": [],
            }
        ],
    )

    assert csv_to_ledger(ledger_to_csv(ledger)) == ledger


def test_csv_is_sorted_by_date() -> None:
    ledger = apply_sync_pages(
        {},
        [
            {
                "added": [
                    _txn("late", date="2026-11-01"),
                    _txn("early", date="2026-09-01"),
                ],
                "modified": [],
                "removed": [],
            }
        ],
    )

    lines = ledger_to_csv(ledger).splitlines()

    assert lines[1].startswith("early,")
    assert lines[2].startswith("late,")


@pytest.mark.parametrize(
    "name", ['=HYPERLINK("http://x")', "+1+1", "-cmd", "@SUM(A1)", "'quoted"]
)
def test_text_that_looks_like_a_formula_survives_a_round_trip(name: str) -> None:
    ledger = apply_sync_pages(
        {}, [{"added": [_txn("t1", name=name)], "modified": [], "removed": []}]
    )

    text = ledger_to_csv(ledger)

    assert csv_to_ledger(text)["t1"]["name"] == name


def test_negative_amounts_are_not_escaped() -> None:
    ledger = apply_sync_pages(
        {}, [{"added": [_txn("t1", -2500.0)], "modified": [], "removed": []}]
    )

    assert ",-2500.00," in ledger_to_csv(ledger)


def test_csv_with_wrong_header_is_rejected() -> None:
    with pytest.raises(ValueError, match="header"):
        csv_to_ledger("id,when\n1,2026-01-01\n")


def test_empty_csv_gives_an_empty_ledger() -> None:
    assert csv_to_ledger("") == {}


def test_non_numeric_amount_raises_value_error() -> None:
    with pytest.raises(ValueError, match="not numeric"):
        apply_sync_pages(
            {}, [{"added": [_txn("t1", amount="abc")], "modified": [], "removed": []}]
        )


# --- Sync run and command line -------------------------------------------------------


def test_first_run_writes_raw_ledger_cursor_and_status(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    pages = [
        {
            "added": [_txn("t1", 10.0)],
            "modified": [],
            "removed": [],
            "next_cursor": "c1",
            "has_more": True,
        },
        {
            "added": [_txn("t2", 5.0)],
            "modified": [],
            "removed": [],
            "next_cursor": "c2",
            "has_more": False,
        },
    ]

    status = run_sync(_settings(), store, lambda _s, _c: pages, "2026-11-26")

    assert status["state"] == "ok"
    assert status["rows"] == 2
    assert status["pages_fetched"] == 2
    assert status["latest_date"] == "2026-10-03"
    assert json.loads(store.read_text(CURSOR_KEY) or "{}") == {"cursor": "c2"}
    assert store.read_text("raw/2026-11-26/sync-000.json") is not None
    assert store.read_text("raw/2026-11-26/sync-001.json") is not None
    assert "t2," in (store.read_text(PROCESSED_KEY) or "")
    assert json.loads(store.read_text(STATUS_KEY) or "{}")["state"] == "ok"


def test_second_run_sends_the_saved_cursor_and_merges_into_the_ledger(
    tmp_path: Path,
) -> None:
    store = LocalStore(tmp_path)
    run_sync(
        _settings(),
        store,
        lambda _s, _c: [
            {
                "added": [_txn("t1", 10.0)],
                "modified": [],
                "removed": [],
                "next_cursor": "c1",
                "has_more": False,
            }
        ],
        "2026-11-26",
    )
    seen_cursors: list[str | None] = []

    def fetch(_settings_arg: Settings, cursor: str | None) -> list[dict[str, Any]]:
        seen_cursors.append(cursor)
        return [
            {
                "added": [_txn("t2", 7.0)],
                "modified": [],
                "removed": [],
                "next_cursor": "c2",
                "has_more": False,
            }
        ]

    status = run_sync(_settings(), store, fetch, "2026-11-27")

    assert seen_cursors == ["c1"]
    assert status["rows"] == 2
    assert json.loads(store.read_text(CURSOR_KEY) or "{}") == {"cursor": "c2"}


def test_failed_fetch_is_recorded_and_re_raised(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)

    def failing_fetch(
        _settings_arg: Settings, _cursor: str | None
    ) -> list[dict[str, Any]]:
        raise PlaidError("Plaid returned HTTP 500 (INTERNAL_SERVER_ERROR)")

    with pytest.raises(PlaidError, match="HTTP 500"):
        run_sync(_settings(), store, failing_fetch, "2026-11-26")

    status = json.loads(store.read_text(STATUS_KEY) or "{}")
    assert status["state"] == "failed"
    assert "HTTP 500" in status["error"]
    assert store.read_text(PROCESSED_KEY) is None
    assert store.read_text(CURSOR_KEY) is None


def test_main_replays_sample_pages_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_STORE_DIR", str(tmp_path))
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    monkeypatch.delenv("STORE_PREFIX", raising=False)

    exit_code = server.main(["--from-sample", str(SAMPLE_PAGES)])

    processed = (tmp_path / "participants" / "demo" / PROCESSED_KEY).read_text(
        encoding="utf-8"
    )
    assert exit_code == 0
    assert processed.count("\n") - 1 == 21
    assert "txn_012" not in processed


def test_main_returns_1_when_live_credentials_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCAL_STORE_DIR", str(tmp_path))
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    for name in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    assert server.main([]) == 1
    status = json.loads(
        (tmp_path / "participants" / "demo" / STATUS_KEY).read_text(encoding="utf-8")
    )
    assert status["state"] == "failed"
