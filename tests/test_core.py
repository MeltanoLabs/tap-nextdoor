"""Tests for tap-nextdoor, run against a mocked NAM API.

The SDK's standard test suite (``get_tap_test_class``) issues live requests,
so it is driven here through the ``nam_api`` fixture from ``conftest.py``
rather than against the real API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import pytest

from tap_nextdoor import streams
from tap_nextdoor.tap import TapNextdoor

if TYPE_CHECKING:
    from tap_nextdoor.client import NextdoorStream

EXPECTED_PAGES = 2

# The conftest window is 2025-01-01..2025-01-31.
JANUARY_DAYS = 31

# The two CSV rows served by the mocked report download, in real live shapes.
EXPECTED_REPORT_ROWS = (
    {
        "impressions": 72914,
        "clicks": 762,
        "ctr": 1.05,  # "1.05%" with the suffix stripped, scale preserved
        "gross_spend": 373.36,
        "billable_spend": 371.93,
    },
    {
        "impressions": 293,
        "clicks": 1,
        "ctr": 0.34,
        "gross_spend": 1.29,
        "billable_spend": 1.29,
    },
)

EXPECTED_STREAMS = {
    "users",
    "profiles",
    "advertisers",
    "campaigns",
    "ad_groups",
    "ads",
    "creatives",
    "reports",
    "ad_stats",
    "performance_report",
    "custom_audiences",
}


def _sync_all(config: dict) -> None:
    """Sync every root stream, which cascades into the child streams."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    for stream in tap.streams.values():
        if stream.parent_stream_type is None:
            stream.sync()


def test_discovery(config: dict) -> None:
    """All documented streams are discovered with a valid catalog."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    catalog = tap.catalog_dict
    assert {s["tap_stream_id"] for s in catalog["streams"]} == EXPECTED_STREAMS


def test_bearer_token_is_sent(config: dict, nam_api) -> None:
    """The access token is sent as an Authorization bearer header."""
    TapNextdoor(config=config, parse_env_config=False).streams["users"].sync()
    assert nam_api.request_history[0].headers["Authorization"] == (
        "Bearer test-access-token"
    )


def test_list_requests_send_a_json_body(config: dict, nam_api) -> None:
    """List endpoints are GET requests carrying advertiser/parent ids and paging."""
    _sync_all(config)

    bodies: dict[str, dict] = {}
    for request in nam_api.request_history:
        # Keep the first request per endpoint - later ones carry a page cursor.
        if request.body and request.path.endswith(
            ("/campaign/list", "/adgroup/list", "/ad/list"),
        ):
            bodies.setdefault(request.path, request.json())
    assert bodies["/v2/api/advertiser/campaign/list"] == {
        "advertiser_id": "adv1",
        "pagination_parameters": {"page_size": 2},
    }
    assert bodies["/v2/api/adgroup/list"]["campaign_id"]
    assert bodies["/v2/api/ad/list"]["adgroup_id"] == "ag1"


def test_cursor_pagination_follows_end_cursor(config: dict, nam_api) -> None:
    """A full page is followed using page_info.end_cursor; a short page ends it."""
    _sync_all(config)

    paged = [
        r.json()
        for r in nam_api.request_history
        if r.path == "/v2/api/advertiser/campaign/list"
    ]
    assert len(paged) == EXPECTED_PAGES, "expected exactly two pages"
    assert "cursor" not in paged[0]["pagination_parameters"]
    assert paged[1]["pagination_parameters"]["cursor"] == "CURSOR_A"


def test_advertisers_come_from_me_and_honour_the_id_filter(
    config: dict,
    nam_api,
) -> None:
    """/me lists two advertisers; the config filter narrows it to one.

    The unfiltered case is covered by
    :func:`test_all_advertisers_are_synced_without_a_filter`.
    """
    TapNextdoor(config=config, parse_env_config=False).streams["advertisers"].sync()

    # Only the selected advertiser has its detail fetched ...
    detail = {
        r.path.rsplit("/", 1)[-1]
        for r in nam_api.request_history
        if "/advertiser/get/" in r.path
    }
    assert detail == {"adv1"}

    # ... and only it is followed into the child streams.
    requested = {
        r.json()["advertiser_id"]
        for r in nam_api.request_history
        if r.path == "/v2/api/advertiser/campaign/list"
    }
    assert requested == {"adv1"}


def test_advertiser_id_is_normalised_from_the_live_key(config: dict, nam_api) -> None:  # noqa: ARG001
    """The live `id` key is surfaced as `advertiser_id`."""
    stream = TapNextdoor(config=config, parse_env_config=False).streams["advertisers"]
    row = stream.post_process({"id": "adv1", "role": "CLIENT_ADMIN"})
    assert row is not None
    assert row == {"advertiser_id": "adv1", "role": "CLIENT_ADMIN"}


def test_zoned_datetimes_are_normalised(config: dict, nam_api) -> None:  # noqa: ARG001
    """Java ZonedDateTime strings lose their bracketed zone id."""
    stream = TapNextdoor(config=config, parse_env_config=False).streams["campaigns"]
    row = stream.post_process(
        {"id": "c1", "start_time": "2025-01-01T00:01:34+01:00[Europe/London]"},
    )
    assert row is not None
    assert row["start_time"] == "2025-01-01T00:01:34+01:00"


def test_audience_ids_are_read_from_targeting(config: dict, nam_api) -> None:  # noqa: ARG001
    """Audience ids come from targeting.custom_audience_targeting when nested."""
    stream = TapNextdoor(config=config, parse_env_config=False).streams["ad_groups"]
    context = stream.get_child_context(
        {
            "id": "ag1",
            "targeting": {
                "custom_audience_targeting": {
                    "include": [["ca1"]],
                    "exclude": [["ca2"]],
                },
            },
        },
        {"advertiser_id": "adv1"},
    )
    assert context is not None
    assert context["custom_audience_ids"] == ["ca1", "ca2"]


def test_all_advertisers_are_synced_without_a_filter(config: dict, nam_api) -> None:
    """With no advertiser_ids set, every advertiser from /me is followed."""
    del config["advertiser_ids"]
    TapNextdoor(config=config, parse_env_config=False).streams["advertisers"].sync()

    requested = {
        r.json()["advertiser_id"]
        for r in nam_api.request_history
        if r.path == "/v2/api/advertiser/campaign/list"
    }
    assert requested == {"adv1", "adv2"}


def test_ad_stats_requests_one_day_at_a_time(config: dict, nam_api) -> None:
    """ad_stats is a daily series: start_time == end_time on every request."""
    TapNextdoor(config=config, parse_env_config=False).streams["advertisers"].sync()

    stats = [
        r.json()
        for r in nam_api.request_history
        if r.path.endswith("/ad/get/ad1/stats")
    ]
    assert stats, "the ad_stats stream did not call the stats endpoint"
    assert stats[0] == {
        "advertiser_id": "adv1",
        "start_time": "2025-01-01",
        "end_time": "2025-01-01",
    }
    # Every request covers exactly one day ...
    assert all(s["start_time"] == s["end_time"] for s in stats)
    # ... and the window 2025-01-01..2025-01-31 is 31 of them, per ad.
    assert max(s["start_time"] for s in stats) == "2025-01-31"
    assert len({s["start_time"] for s in stats}) == JANUARY_DAYS


def test_custom_audiences_are_fetched_once_per_id(config: dict, nam_api) -> None:
    """Audiences shared across ad groups are only requested once."""
    TapNextdoor(config=config, parse_env_config=False).streams["advertisers"].sync()

    calls = [
        r
        for r in nam_api.request_history
        if r.path.endswith("/custom_audience/get/ca1")
    ]
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("2026-01-01T00:00:00Z", "2026-01-01"),
        ("2026-01-01t00:00:00z", "2026-01-01"),
        ("2026-01-01", "2026-01-01"),
        ("2026-01-01T23:30:00+01:00", "2026-01-01"),
        ("2026-01-01T00:00:00", "2026-01-01"),
    ],
)
def test_start_date_accepts_iso8601_date_times(
    config: dict,
    nam_api,
    configured: str,
    expected: str,
) -> None:
    """start_date accepts a full ISO-8601 date-time, not just a date.

    Asserted against the /stats endpoint, which takes a LocalDate and so
    truncates any time component. The "Z" suffix matters:
    datetime.fromisoformat only accepts it from Python 3.11, and this package
    supports 3.10.
    """
    config["start_date"] = configured
    # ad_stats now walks day by day, so keep the window to that single day.
    config["end_date"] = configured
    tap = TapNextdoor(config=config, parse_env_config=False)
    tap.streams["advertisers"].sync()

    body = next(
        r.json()
        for r in nam_api.request_history
        if r.path.endswith("/ad/get/ad1/stats")
    )
    assert body["start_time"] == expected


def test_unparseable_start_date_is_rejected_clearly(config: dict, nam_api) -> None:  # noqa: ARG001
    """A malformed date names the accepted formats rather than raising raw."""
    config["start_date"] = "01/01/2026"
    stream = cast(
        "NextdoorStream",
        TapNextdoor(config=config, parse_env_config=False).streams["ad_stats"],
    )
    with pytest.raises(ValueError, match="ISO-8601 date or date-time"):
        stream.window_date("start_date")


def test_ad_stats_still_uses_plain_local_dates(config: dict, nam_api) -> None:
    """The /stats endpoints take a LocalDate, unlike reporting/create."""
    TapNextdoor(config=config, parse_env_config=False).streams["advertisers"].sync()

    stats = next(
        r.json()
        for r in nam_api.request_history
        if r.path.endswith("/ad/get/ad1/stats")
    )
    assert stats["start_time"] == "2025-01-01"
    assert stats["end_time"] == "2025-01-01"


def test_ad_stats_stamps_the_day_on_each_record(config: dict, nam_api) -> None:  # noqa: ARG001
    """Each row carries the day it covers, which is the replication key."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = cast("NextdoorStream", tap.streams["ad_stats"])
    rows = list(stream.get_records({"advertiser_id": "adv1", "ad_id": "ad1"}))

    assert len(rows) == JANUARY_DAYS
    assert [r["date"] for r in rows][:3] == [
        "2025-01-01",
        "2025-01-02",
        "2025-01-03",
    ]


def test_ad_stats_resumes_from_the_bookmark_with_a_lookback(
    config: dict,
    nam_api,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incremental run restarts `lookback_days` before the bookmark.

    Ad metrics are restated as conversions are attributed late, so recent days
    are deliberately re-fetched rather than trusted as final.
    """
    config["lookback_days"] = 3
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = cast("NextdoorStream", tap.streams["ad_stats"])
    # Stand in for a stored bookmark, rather than hand-building state JSON.
    monkeypatch.setattr(
        stream,
        "get_starting_replication_key_value",
        lambda _context: "2025-01-20",
    )
    days = [
        r["date"] for r in stream.get_records({"advertiser_id": "adv1", "ad_id": "ad1"})
    ]

    assert days[0] == "2025-01-17", "should rewind 3 days before the bookmark"
    assert days[-1] == "2025-01-31"


def test_ad_stats_warns_when_the_window_is_empty(
    config: dict,
    nam_api,  # noqa: ARG001
    caplog,
) -> None:
    """A start_date after end_date syncs nothing, and says so."""
    config["start_date"] = "2026-01-01"
    config["end_date"] = "2025-01-31"
    stream = cast(
        "NextdoorStream",
        TapNextdoor(config=config, parse_env_config=False).streams["ad_stats"],
    )
    with caplog.at_level(logging.WARNING):
        rows = list(stream.get_records({"advertiser_id": "adv1", "ad_id": "ad1"}))

    assert rows == []
    assert "is after end_date" in caplog.text


def test_advertisers_are_enriched_with_their_detail(config: dict, nam_api) -> None:  # noqa: ARG001
    """The undocumented /advertiser/get/{id} fills in name, currency, timezone."""
    stream = cast(
        "NextdoorStream",
        TapNextdoor(config=config, parse_env_config=False).streams["advertisers"],
    )
    rows = [
        stream.post_process(cast("dict", row), {"advertiser_id": "adv1"})
        for row in stream.get_records({"advertiser_id": "adv1"})
    ]
    row = rows[0]
    assert row is not None
    assert row["advertiser_id"] == "adv1"
    assert row["name"] == "Acme"
    assert row["currency"] == "GBP"
    assert row["timezone"] == "Europe/London"
    assert row["address"]["country"] == "GB"
    # role comes from /me, not from the advertiser record
    assert row["role"] == "CLIENT_ADMIN"
    # the raw `id` key is replaced, not carried through
    assert "id" not in row


def test_unreachable_advertiser_ids_are_reported(config: dict, nam_api, caplog) -> None:  # noqa: ARG001
    """Configuring an advertiser the token cannot see warns rather than failing."""
    config["advertiser_ids"] = ["adv1", "nope"]
    stream = cast(
        "NextdoorStream",
        TapNextdoor(config=config, parse_env_config=False).streams["advertisers"],
    )
    with caplog.at_level(logging.WARNING):
        partitions = stream.partitions

    assert partitions == [{"advertiser_id": "adv1"}]
    assert "not accessible" in caplog.text


# ---------------------------------------------------------------------------
# performance_report - the v3 report builder
# ---------------------------------------------------------------------------

REPORTS_PATH = "/api/v3/advertisers/adv1/reports"


def _create_bodies(nam_api) -> list[dict]:
    """Return the body of every create-report request made."""
    return [
        r.json()
        for r in nam_api.request_history
        if r.path == REPORTS_PATH and r.method == "POST"
    ]


def test_report_is_advertiser_scoped_by_path(config: dict, nam_api) -> None:
    """The advertiser is in the URL, not the body, on the /api/v3 base."""
    config["report"] = {}
    _sync_all(config)

    created = [
        r for r in nam_api.request_history if r.method == "POST" and "/api/v3/" in r.url
    ]
    assert [r.url for r in created] == [
        "https://ads.nextdoor.com/api/v3/advertisers/adv1/reports",
    ]
    # The advertiser is in the path, so it must not also be in the body.
    assert "advertiser_id" not in created[0].json()


def test_report_definition_comes_from_config(config: dict, nam_api) -> None:
    """The body is built from the `report` block, with a nested window."""
    config["report"] = {
        "metrics": ["IMPRESSIONS", "CLICKS"],
        "dimensions": ["DAY", "CREATIVE_ID"],
        "name": "My report",
        "type": "DELIVERY_METRICS_REPORT",
        "recipient_emails": ["a@example.com"],
        "filters": [{"attribute": "CAMPAIGN", "options": ["Brand"]}],
    }
    _sync_all(config)

    assert _create_bodies(nam_api) == [
        {
            # The window is stamped in because the API puts neither a date
            # range nor a timestamp on a report object, and they accumulate.
            "name": "My report 2025-01-01..2025-01-31",
            "type": "DELIVERY_METRICS_REPORT",
            "output_format": "CSV",
            "date_time_range": {
                "start_date_time": "2025-01-01T00:00:00+00:00",
                # end_date is inclusive, so the bound is advanced a day.
                "end_date_time": "2025-02-01T00:00:00+00:00",
            },
            "dimensions": ["DAY", "CREATIVE_ID"],
            "metrics": ["IMPRESSIONS", "CLICKS"],
            "recipient_emails": ["a@example.com"],
            "filters": [
                {"attribute": "CAMPAIGN", "operator": "CONTAINS", "options": ["Brand"]},
            ],
        },
    ]


def test_report_defaults_to_day_and_ad_with_the_delivery_metrics(
    config: dict,
    nam_api,
) -> None:
    """With no config the report is DAY x ad, on the delivery metrics."""
    config["report"] = {}
    _sync_all(config)

    body = _create_bodies(nam_api)[0]
    assert body["dimensions"] == ["DAY", "AD_ID", "AD"]
    assert body["metrics"] == list(streams.REPORT_DEFAULT_METRICS)
    # Not the whole enum - see REPORT_DEFAULT_METRICS for why.
    assert len(body["metrics"]) < len(streams.REPORT_METRICS)


def test_report_is_polled_until_completed(config: dict, nam_api) -> None:
    """A report created as STARTED is polled until COMPLETED, then downloaded.

    The reference presents creation as synchronous, but its status enum
    includes STARTED and IN_PROGRESS, so the stream must not assume the
    download_url is usable on the first response.
    """
    nam_api.post(
        f"https://ads.nextdoor.com{REPORTS_PATH}",
        json={"id": "rep1", "advertiser_id": "adv1", "status": "STARTED"},
    )
    nam_api.get(
        f"https://ads.nextdoor.com{REPORTS_PATH}/rep1",
        [
            {"json": {"id": "rep1", "advertiser_id": "adv1", "status": "IN_PROGRESS"}},
            {
                "json": {
                    "id": "rep1",
                    "advertiser_id": "adv1",
                    "status": "COMPLETED",
                    "download_url": "https://example.com/report.csv",
                },
            },
        ],
    )
    config["report"] = {}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    records = list(stream.get_records({"advertiser_id": "adv1"}))

    polls = [
        r
        for r in nam_api.request_history
        if r.path == f"{REPORTS_PATH}/rep1" and r.method == "GET"
    ]
    # IN_PROGRESS, then COMPLETED.
    assert len(polls) == EXPECTED_PAGES
    assert len(records) == EXPECTED_PAGES


def test_completed_report_is_not_polled(config: dict, nam_api) -> None:
    """A report already COMPLETED on creation is downloaded without polling."""
    config["report"] = {}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    list(stream.get_records({"advertiser_id": "adv1"}))

    assert not [
        r
        for r in nam_api.request_history
        if r.path == f"{REPORTS_PATH}/rep1" and r.method == "GET"
    ]


def test_report_csv_is_parsed_into_records(config: dict, nam_api) -> None:  # noqa: ARG001
    """The downloaded CSV becomes records, including the creative columns."""
    config["report"] = {
        "dimensions": ["DAY", "AD_ID", "AD", "CREATIVE_ID", "CREATIVE"],
        "metrics": ["IMPRESSIONS", "CLICKS", "CTR", "SPEND"],
    }
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    records = list(stream.get_records({"advertiser_id": "adv1"}))
    records = [stream.post_process(r, {"advertiser_id": "adv1"}) for r in records]

    assert len(records) == EXPECTED_PAGES
    first = records[0]
    assert first["advertiser_id"] == "adv1"
    assert first["report_id"] == "rep1"
    assert first["date"] == "2026-07-01"
    assert first["ad_id"] == "ad1"
    # v3 returns "Ad"/"Creative", not "Ad Name"/"Creative Name" as v2 does.
    assert first["ad"] == "Ad"
    assert first["creative"] == "Creative"
    # The creative breakdown, which the older v2 report builder cannot produce.
    assert first["creative_id"] == "cr1"
    assert first["impressions"] == EXPECTED_REPORT_ROWS[0]["impressions"]
    assert first["clicks"] == EXPECTED_REPORT_ROWS[0]["clicks"]
    # CTR keeps the percentage scale, matching ad_stats.
    assert first["ctr"] == EXPECTED_REPORT_ROWS[0]["ctr"]
    assert first["gross_spend"] == EXPECTED_REPORT_ROWS[0]["gross_spend"]


def test_report_primary_key_follows_dimensions(config: dict) -> None:
    """One key column per dimension family, preferring the id over the name."""
    config["report"] = {
        "dimensions": ["DAY", "CAMPAIGN_ID", "CAMPAIGN", "CREATIVE_ID", "GENDER"],
        # Explicit, because the default list carries BILLABLE_SPEND, which
        # conflicts with CREATIVE_ID.
        "metrics": ["IMPRESSIONS"],
    }
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    assert tuple(stream.primary_keys) == (
        "advertiser_id",
        "date",
        "campaign_id",
        "creative_id",
        "gender",
    )


def test_report_time_dimensions_share_one_date_column(config: dict) -> None:
    """DAY, WEEK and MONTH all land in `date`, so it is declared once."""
    config["report"] = {"dimensions": ["DAY", "WEEK", "AD_ID"]}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    assert tuple(stream.primary_keys) == ("advertiser_id", "date", "ad_id")
    assert stream.schema["properties"]["date"]["format"] == "date"


def test_window_days_splits_the_run_into_one_report_per_slice(
    config: dict,
    nam_api,
) -> None:
    """A window longer than window_days becomes several smaller reports."""
    # conftest window is 2025-01-01..2025-01-31, i.e. 31 days.
    config["report"] = {"dimensions": ["DAY", "AD_ID"], "window_days": 10}
    _sync_all(config)

    ranges = [b["date_time_range"] for b in _create_bodies(nam_api)]
    # 31 days in slices of 10 -> 10 + 10 + 10 + 1. Each end bound is advanced
    # a day, because end_date is inclusive.
    assert ranges == [
        {
            "start_date_time": "2025-01-01T00:00:00+00:00",
            "end_date_time": "2025-01-11T00:00:00+00:00",
        },
        {
            "start_date_time": "2025-01-11T00:00:00+00:00",
            "end_date_time": "2025-01-21T00:00:00+00:00",
        },
        {
            "start_date_time": "2025-01-21T00:00:00+00:00",
            "end_date_time": "2025-01-31T00:00:00+00:00",
        },
        {
            "start_date_time": "2025-01-31T00:00:00+00:00",
            "end_date_time": "2025-02-01T00:00:00+00:00",
        },
    ]


def test_window_days_unset_builds_one_report(config: dict, nam_api) -> None:
    """Without window_days the whole window goes in a single report."""
    config["report"] = {}
    _sync_all(config)

    assert [b["date_time_range"] for b in _create_bodies(nam_api)] == [
        {
            "start_date_time": "2025-01-01T00:00:00+00:00",
            "end_date_time": "2025-02-01T00:00:00+00:00",
        },
    ]


def test_report_is_incremental_on_date_when_dimensions_carry_day(
    config: dict,
) -> None:
    """A DAY report replicates incrementally on the `date` column."""
    config["report"] = {"dimensions": ["DAY", "AD_ID"]}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]

    assert stream.replication_key == "date"
    assert stream.replication_method == "INCREMENTAL"
    # The bookmark column has to be in the key too, or the target cannot
    # upsert the restated rows a lookback re-emits.
    assert "date" in stream.primary_keys


@pytest.mark.parametrize(
    "dimensions",
    [
        pytest.param(["AD_ID", "AD"], id="no-time-bucket"),
        # WEEK/MONTH bucket values are unverified on this endpoint - see
        # _INCREMENTAL_TIME_BUCKET.
        pytest.param(["WEEK", "AD_ID"], id="week"),
        pytest.param(["MONTH", "AD_ID"], id="month"),
    ],
)
def test_report_is_full_table_without_a_day_dimension(
    config: dict,
    dimensions: list[str],
) -> None:
    """Anything but DAY leaves the stream on FULL_TABLE."""
    config["report"] = {"dimensions": dimensions}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]

    assert stream.replication_key is None
    assert stream.replication_method == "FULL_TABLE"


def test_report_resumes_from_the_bookmark_with_a_lookback(
    config: dict,
    nam_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incremental run asks for a shorter window, not the whole history.

    Generation time grows with the window, so this is the stream's main cost
    lever. `lookback_days` of synced history is re-requested, because report
    metrics are restated as conversions are attributed late.
    """
    config["lookback_days"] = 3
    config["report"] = {"dimensions": ["DAY", "AD_ID"]}
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = cast("NextdoorStream", tap.streams["performance_report"])
    # Stand in for a stored bookmark, rather than hand-building state JSON.
    monkeypatch.setattr(
        stream,
        "get_starting_replication_key_value",
        lambda _context: "2025-01-20",
    )
    list(stream.get_records({"advertiser_id": "adv1"}))

    assert [b["date_time_range"] for b in _create_bodies(nam_api)] == [
        {
            # 3 days before the bookmark, not the configured 2025-01-01.
            "start_date_time": "2025-01-17T00:00:00+00:00",
            "end_date_time": "2025-02-01T00:00:00+00:00",
        },
    ]


def test_report_bookmark_only_moves_the_start_forward(
    config: dict,
    nam_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bookmark earlier than start_date does not widen the window.

    Otherwise lowering start_date would be undone by an old bookmark, and the
    tap would generate a report for a period the user excluded.
    """
    config["lookback_days"] = 7
    config["report"] = {"dimensions": ["DAY", "AD_ID"]}
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = cast("NextdoorStream", tap.streams["performance_report"])
    monkeypatch.setattr(
        stream,
        "get_starting_replication_key_value",
        lambda _context: "2025-01-02",
    )
    list(stream.get_records({"advertiser_id": "adv1"}))

    starts = [b["date_time_range"]["start_date_time"] for b in _create_bodies(nam_api)]
    assert starts == ["2025-01-01T00:00:00+00:00"]


def test_report_syncs_nothing_when_the_bookmark_passes_end_date(
    config: dict,
    nam_api,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    """A bookmark past end_date creates no report, and says why."""
    config["lookback_days"] = 0
    config["report"] = {"dimensions": ["DAY", "AD_ID"]}
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = cast("NextdoorStream", tap.streams["performance_report"])
    monkeypatch.setattr(
        stream,
        "get_starting_replication_key_value",
        lambda _context: "2025-03-01",
    )
    with caplog.at_level(logging.WARNING):
        assert list(stream.get_records({"advertiser_id": "adv1"})) == []

    assert "nothing to sync" in caplog.text
    assert not _create_bodies(nam_api), "no report object should be created"


def test_window_days_requires_a_time_dimension(config: dict) -> None:
    """Slicing without DAY/WEEK/MONTH would collide every slice's keys."""
    config["report"] = {"dimensions": ["AD_ID"], "window_days": 10}
    with pytest.raises(ValueError, match="must include a time bucket"):
        TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018


def test_window_days_must_be_positive(config: dict) -> None:
    """A negative slice size is rejected rather than looping forever."""
    config["report"] = {"dimensions": ["DAY", "AD_ID"], "window_days": -1}
    with pytest.raises(ValueError, match="at least 1"):
        TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018


def test_empty_report_logs_its_csv_header(config: dict, nam_api, caplog) -> None:
    """A zero-row report logs the header, so column names can be confirmed.

    An empty report is not an error, but the target writes no file for it,
    which looks identical to a broken sync. The header row survives an empty
    report and is the only place the CSV's real column names appear.
    """
    nam_api.get(
        "https://example.com/report.csv",
        text="Date,Ad Id,Creative Id,Impressions\n",
    )
    config["report"] = {}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    with caplog.at_level(logging.WARNING):
        assert list(stream.get_records({"advertiser_id": "adv1"})) == []

    assert "0 data rows" in caplog.text
    # Both the raw header and what it normalises to, since the mapping is
    # what needs correcting when a guess is wrong.
    assert "'Creative Id'" in caplog.text
    assert "creative_id" in caplog.text


def test_non_numeric_metric_cell_becomes_null(config: dict, nam_api) -> None:  # noqa: ARG001
    """A numeric column holding "N/A" nulls that cell, not the whole sync.

    Seen live: "N/A" appeared partway through a report and took a ten-minute
    sync down with a ValueError, discarding every row already fetched.
    """
    config["report"] = {
        "dimensions": ["DAY", "AD_ID"],
        "metrics": ["IMPRESSIONS", "SPEND"],
    }
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    records = [
        stream.post_process(r, {"advertiser_id": "adv1"})
        for r in stream.get_records({"advertiser_id": "adv1"})
    ]

    assert len(records) == EXPECTED_PAGES
    assert records[0]["gross_spend"] == EXPECTED_REPORT_ROWS[0]["gross_spend"]
    # The second row's Gross Spend is "N/A" in the fixture.
    assert records[1]["gross_spend"] is None
    # The rest of that row survives.
    assert records[1]["impressions"] == EXPECTED_REPORT_ROWS[1]["impressions"]


def test_unknown_non_numeric_is_nulled_with_a_warning(config: dict, caplog) -> None:
    """An unrecognised placeholder warns rather than raising."""
    config["report"] = {"dimensions": ["DAY"], "metrics": ["IMPRESSIONS"]}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    with caplog.at_level(logging.WARNING):
        assert stream._as_number("IMPRESSIONS", "impressions", "roughly 12") is None  # noqa: SLF001
    assert "is not a number" in caplog.text
    # Known placeholders are silent.
    with caplog.at_level(logging.WARNING):
        caplog.clear()
        assert stream._as_number("IMPRESSIONS", "impressions", "N/A") is None  # noqa: SLF001
    assert caplog.text == ""


def test_report_name_carries_the_window(config: dict, nam_api) -> None:
    """The window is stamped into the NAM report name.

    The API exposes neither a created-at nor a date range on a report, and the
    objects accumulate in the account, so the name is the only thing that can
    identify the period one covers.
    """
    config["report"] = {"dimensions": ["DAY", "AD_ID"], "window_days": 20}
    _sync_all(config)

    assert [b["name"] for b in _create_bodies(nam_api)] == [
        "performance report 2025-01-01..2025-01-20",
        "performance report 2025-01-21..2025-01-31",
    ]


def test_stream_name_is_unaffected_by_the_window_stamp(config: dict) -> None:
    """Stamping the window into the report name must not rename the table."""
    config["report"] = {"name": "Ad Performance Report"}
    tap = TapNextdoor(config=config, parse_env_config=False)
    assert "ad_performance_report" in tap.streams


def test_conflicting_dimension_and_metric_are_rejected(config: dict) -> None:
    """CREATIVE_ID with BILLABLE_SPEND fails at startup, not mid-sync.

    The API rejects the pair with REPORT_BUILDER_CONFLICT_PARAMETER: billable
    spend attaches above the creative, so it cannot be split per creative.
    """
    config["report"] = {
        "dimensions": ["DAY", "CREATIVE_ID"],
        "metrics": ["IMPRESSIONS", "BILLABLE_SPEND"],
    }
    with pytest.raises(ValueError, match="REPORT_BUILDER_CONFLICT_PARAMETER") as spelt:
        TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018
    assert "from report.metrics" in str(spelt.value)


def test_conflict_from_the_default_metrics_names_the_default(config: dict) -> None:
    """Asking only for CREATIVE_ID still conflicts, via the default metrics.

    The SDK fills the config_jsonschema default in, so the message must blame
    the default list rather than a `report.metrics` the user never wrote.
    """
    config["report"] = {"dimensions": ["DAY", "CREATIVE_ID"]}
    with pytest.raises(ValueError, match="REPORT_BUILDER_CONFLICT_PARAMETER") as spelt:
        TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018
    assert "the default metric list" in str(spelt.value)


def test_creative_id_allows_gross_spend(config: dict) -> None:
    """Only BILLABLE_SPEND conflicts with CREATIVE_ID; SPEND is fine."""
    config["report"] = {
        "dimensions": ["DAY", "CREATIVE_ID"],
        "metrics": ["IMPRESSIONS", "SPEND"],
    }
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    assert "gross_spend" in stream.schema["properties"]


def test_retired_v2_report_settings_are_rejected_with_their_replacement(
    config: dict,
) -> None:
    """A config written for the old v2 builder fails loudly, not silently.

    The v3 endpoint has no equivalent of dimension_granularity,
    time_granularity or the *_ids filters, so a carried-over config would
    otherwise be ignored and the report quietly built with defaults.
    """
    config["report"] = {"dimension_granularity": ["AD_ID"], "ad_ids": ["ad1"]}
    with pytest.raises(ValueError, match=r"not supported by the v3 report") as excinfo:
        TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018

    message = str(excinfo.value)
    # The error names both offenders and what replaces each.
    assert "report.dimension_granularity -> use report.dimensions" in message
    assert "report.ad_ids -> use report.filters" in message


def test_invalid_report_values_are_rejected(config: dict) -> None:
    """Bad metrics, dimensions, types and filters all fail with a clear message."""
    for block, pattern in (
        ({"metrics": ["NOPE"]}, r"Invalid report\.metrics"),
        ({"dimensions": ["NOPE"]}, r"Invalid report\.dimensions"),
        ({"type": "NOPE"}, r"Invalid report\.type"),
        (
            {"filters": [{"attribute": "CREATIVE"}]},
            r"Invalid report\.filters\[\]\.attribute",
        ),
    ):
        config["report"] = block
        with pytest.raises(ValueError, match=pattern):
            TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018


def test_failed_report_raises_rather_than_syncing_nothing(
    config: dict,
    nam_api,
) -> None:
    """A report that ends FAILED fails the sync instead of emitting zero rows."""
    nam_api.post(
        f"https://ads.nextdoor.com{REPORTS_PATH}",
        json={"id": "rep1", "advertiser_id": "adv1", "status": "STARTED"},
    )
    nam_api.get(
        f"https://ads.nextdoor.com{REPORTS_PATH}/rep1",
        json={"id": "rep1", "advertiser_id": "adv1", "status": "FAILED"},
    )
    config["report"] = {}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    with pytest.raises(RuntimeError, match="finished with status FAILED"):
        list(stream.get_records({"advertiser_id": "adv1"}))


def test_poll_timeout_raises(
    config: dict,
    nam_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report still running at the ceiling fails with actionable advice.

    The advice has to be actionable *without* a setting to raise, since the
    ceiling is a module constant now: a report still running at 30 minutes
    wants a narrower window, not a longer wait.
    """
    nam_api.post(
        f"https://ads.nextdoor.com{REPORTS_PATH}",
        json={"id": "rep1", "advertiser_id": "adv1", "status": "IN_PROGRESS"},
    )
    nam_api.get(
        f"https://ads.nextdoor.com{REPORTS_PATH}/rep1",
        json={"id": "rep1", "advertiser_id": "adv1", "status": "IN_PROGRESS"},
    )
    monkeypatch.setattr(streams, "MAX_POLL_SECONDS", 0)
    config["report"] = {}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    with pytest.raises(RuntimeError, match="Narrow the window"):
        list(stream.get_records({"advertiser_id": "adv1"}))


def test_poll_settings_are_not_config(config: dict) -> None:
    """The poll interval and ceiling are constants, not `report` settings.

    They were config once. A stale config carrying them must not look like it
    is being honoured, and must not fail either - the SDK would reject an
    unknown key only if the schema forbade extras.
    """
    assert (
        "poll_interval_seconds"
        not in TapNextdoor.config_jsonschema["properties"]["report"]["properties"]
    )
    assert (
        "max_poll_seconds"
        not in TapNextdoor.config_jsonschema["properties"]["report"]["properties"]
    )

    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    assert "poll_interval_seconds" not in stream.report_config
    assert "max_poll_seconds" not in stream.report_config


def test_stream_name_is_configurable(config: dict) -> None:
    """report.stream_name renames the stream, so it can match the granularity."""
    default = TapNextdoor(config=config, parse_env_config=False)
    assert "performance_report" in default.streams

    config["report"] = {"stream_name": "creative_performance_report"}
    renamed = TapNextdoor(config=config, parse_env_config=False)
    assert "creative_performance_report" in renamed.streams
    assert "performance_report" not in renamed.streams
