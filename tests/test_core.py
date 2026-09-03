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


def test_report_definition_comes_from_config(config: dict, nam_api) -> None:
    """The report body is built from the `report` config block."""
    config["report"] = {
        "metrics": ["IMPRESSIONS", "CLICKS", "SPEND"],
        "dimension_granularity": ["CAMPAIGN", "AD"],
        "time_granularity": ["DAY"],
        "name": "My report",
        "recipient_emails": ["someone@example.com"],
        "campaign_ids": ["camp1"],
    }
    tap = TapNextdoor(config=config, parse_env_config=False)
    tap.streams["advertisers"].sync()

    created = [
        r.json()
        for r in nam_api.request_history
        if r.path == "/v2/api/reporting/create"
    ]
    assert created[0] == {
        "advertiser_id": "adv1",
        "name": "My report",
        "recipient_emails": ["someone@example.com"],
        "dimension_granularity": ["CAMPAIGN", "AD"],
        "time_granularity": ["DAY"],
        "metrics": ["IMPRESSIONS", "CLICKS", "SPEND"],
        # Offset-bearing date-times, and an exclusive upper bound one day
        # past the inclusive end_date setting.
        "start_time": "2025-01-01T00:00:00+00:00",
        "end_time": "2025-02-01T00:00:00+00:00",
        "campaign_ids": ["camp1"],
    }


def test_report_defaults_to_all_metrics_by_ad_and_day(config: dict, nam_api) -> None:
    """With no report config, every metric is requested by ad id/name and DAY."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    tap.streams["advertisers"].sync()

    body = next(
        r.json()
        for r in nam_api.request_history
        if r.path == "/v2/api/reporting/create"
    )
    assert body["metrics"] == list(streams.REPORT_METRICS)
    assert body["dimension_granularity"] == ["AD_ID", "AD"]
    assert body["time_granularity"] == ["DAY"]
    assert body["recipient_emails"] == []


def test_report_csv_is_parsed_into_records(config: dict, nam_api) -> None:  # noqa: ARG001
    """CSV headers are snake_cased and numeric metrics are cast."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = tap.streams["performance_report"]
    rows = [
        stream.post_process(cast("dict", row), {"advertiser_id": "adv1"})
        for row in stream.get_records({"advertiser_id": "adv1"})
    ]
    assert len(rows) == len(EXPECTED_REPORT_ROWS)
    first = rows[0]
    assert first is not None
    assert first["date"] == "2026-07-01"
    assert first["ad_name"] == "Ad"
    assert first["ad_id"] == "ad1"
    assert first["impressions"] == EXPECTED_REPORT_ROWS[0]["impressions"]
    assert first["clicks"] == EXPECTED_REPORT_ROWS[0]["clicks"]
    assert first["ctr"] == EXPECTED_REPORT_ROWS[0]["ctr"]
    # The report CSV carries bare decimals, unlike the /stats endpoint.
    assert first["gross_spend"] == EXPECTED_REPORT_ROWS[0]["gross_spend"]
    assert first["report_id"] == "rep1"


def test_invalid_report_metric_is_rejected(config: dict) -> None:
    """A misspelled metric fails fast with the supported values listed."""
    config["report"] = {"metrics": ["IMPRESSIONS", "SPENDD"]}
    with pytest.raises(ValueError, match=r"Invalid report\.metrics"):
        TapNextdoor(config=config, parse_env_config=False).streams  # noqa: B018


def test_report_primary_key_follows_dimensions(config: dict) -> None:
    """The key is advertiser + time bucket + one id per requested dimension."""
    config["report"] = {"dimension_granularity": ["CAMPAIGN", "AD_GROUP"]}
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    assert tuple(stream.primary_keys) == (
        "advertiser_id",
        "date",
        "campaign_name",
        "ad_group_name",
    )


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


def test_report_money_metrics_are_numeric(config: dict, nam_api) -> None:  # noqa: ARG001
    """Report spend is a bare decimal, unlike the /stats endpoint's "GBP 0"."""
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    rows = [
        stream.post_process(cast("dict", row), {"advertiser_id": "adv1"})
        for row in stream.get_records({"advertiser_id": "adv1"})
    ]
    first = rows[0]
    assert first is not None
    expected = EXPECTED_REPORT_ROWS[0]
    assert first["gross_spend"] == expected["gross_spend"]
    assert first["billable_spend"] == expected["billable_spend"]
    assert first["ctr"] == expected["ctr"]
    assert first["impressions"] == expected["impressions"]
    assert isinstance(first["impressions"], int)


def test_report_columns_are_names_not_ids(config: dict, nam_api) -> None:  # noqa: ARG001
    """The report CSV carries dimension names only - it reports no IDs."""
    config["report"] = {"dimension_granularity": ["CAMPAIGN", "AD_GROUP", "AD"]}
    props = (
        TapNextdoor(config=config, parse_env_config=False)
        .streams["performance_report"]
        .schema["properties"]
    )
    assert {"campaign_name", "ad_group_name", "ad_name"} <= set(props)
    assert not {"campaign_id", "ad_group_id", "adgroup_id", "ad_id"} & set(props)


def test_report_ctr_keeps_its_percentage_scale(config: dict, nam_api) -> None:  # noqa: ARG001
    """CTR arrives as "1.05%"; the suffix is stripped but the scale is kept.

    ad_stats reports CTR on the same percentage scale - 0.5573934 for an ad
    with 246 clicks on 44,134 impressions - so rescaling here would make the
    two performance streams disagree.
    """
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "performance_report"
    ]
    rows = [
        stream.post_process(cast("dict", row), {"advertiser_id": "adv1"})
        for row in stream.get_records({"advertiser_id": "adv1"})
    ]
    first = rows[0]
    assert first is not None
    assert first["ctr"] == EXPECTED_REPORT_ROWS[0]["ctr"]


def test_report_window_is_an_offset_bearing_datetime(config: dict, nam_api) -> None:
    """reporting/create rejects bare dates, so the window must carry an offset.

    Verified against the live API: "2026-07-01" fails with "could not be
    parsed at index 10" and "2026-07-01T00:00:00" fails at index 19.
    """
    config["start_date"] = "2026-07-01"
    config["end_date"] = "2026-07-31"
    TapNextdoor(config=config, parse_env_config=False).streams["advertisers"].sync()

    body = next(
        r.json()
        for r in nam_api.request_history
        if r.path == "/v2/api/reporting/create"
    )
    assert body["start_time"] == "2026-07-01T00:00:00+00:00"
    # end_date is inclusive, so the exclusive upper bound is the next day.
    assert body["end_time"] == "2026-08-01T00:00:00+00:00"


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


def test_stream_name_is_configurable(config: dict) -> None:
    """report.stream_name renames the stream, so it can match the granularity."""
    default = TapNextdoor(config=config, parse_env_config=False)
    assert "performance_report" in default.streams

    config["report"] = {
        "dimension_granularity": ["CAMPAIGN"],
        "stream_name": "campaign_performance_report",
    }
    renamed = TapNextdoor(config=config, parse_env_config=False)
    assert "campaign_performance_report" in renamed.streams
    assert "performance_report" not in renamed.streams


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
