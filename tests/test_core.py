"""Tests for tap-nextdoor, run against a mocked NAM API.

The SDK's standard test suite (``get_tap_test_class``) issues live requests,
so it is driven here through the ``nam_api`` fixture from ``conftest.py``
rather than against the real API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from tap_nextdoor.tap import TapNextdoor

if TYPE_CHECKING:
    from tap_nextdoor.client import NextdoorStream

EXPECTED_PAGES = 2

# The two CSV rows served by the mocked report download, in real live shapes.
EXPECTED_REPORT_ROWS = (
    {
        "impressions": 72914,
        "clicks": 762,
        "ctr": 0.0105,  # "1.05%" converted to a fraction
        "gross_spend": 373.36,
        "billable_spend": 371.93,
    },
    {
        "impressions": 293,
        "clicks": 1,
        "ctr": 0.0034,
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
    "ad_performance_reports",
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
    """/me lists two advertisers; the config filter narrows it to one."""
    tap = TapNextdoor(config=config, parse_env_config=False)

    # /me reports both advertisers, keyed by `id` as the live API does ...
    advertisers = cast("list[dict]", list(tap.streams["advertisers"].get_records(None)))
    assert [a["id"] for a in advertisers] == ["adv1", "adv2"]

    # ... but only the selected one is followed into the child streams.
    tap.streams["advertisers"].sync()
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


def test_ad_performance_sends_the_reporting_window(config: dict, nam_api) -> None:
    """The stats endpoint receives LocalDate start/end times from config."""
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
        "end_time": "2025-01-31",
    }


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
    """With no report config, every metric is requested by AD and DAY."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    tap.streams["advertisers"].sync()

    body = next(
        r.json()
        for r in nam_api.request_history
        if r.path == "/v2/api/reporting/create"
    )
    assert body["metrics"] == [
        "IMPRESSIONS",
        "CLICKS",
        "CTR",
        "SPEND",
        "BILLABLE_SPEND",
        "CPM",
        "CPC",
        "CONVERSIONS",
    ]
    assert body["dimension_granularity"] == ["AD"]
    assert body["time_granularity"] == ["DAY"]
    assert body["recipient_emails"] == []


def test_report_csv_is_parsed_into_records(config: dict, nam_api) -> None:  # noqa: ARG001
    """CSV headers are snake_cased and numeric metrics are cast."""
    tap = TapNextdoor(config=config, parse_env_config=False)
    stream = tap.streams["ad_performance_reports"]
    rows = [
        stream.post_process(cast("dict", row), {"advertiser_id": "adv1"})
        for row in stream.get_records({"advertiser_id": "adv1"})
    ]
    assert len(rows) == len(EXPECTED_REPORT_ROWS)
    first = rows[0]
    assert first is not None
    assert first["date"] == "2026-07-01"
    assert first["ad_name"] == "Ad"
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
        "ad_performance_reports"
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
        "ad_performance_reports"
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
        .streams["ad_performance_reports"]
        .schema["properties"]
    )
    assert {"campaign_name", "ad_group_name", "ad_name"} <= set(props)
    assert not {"campaign_id", "ad_group_id", "adgroup_id", "ad_id"} & set(props)


def test_report_ctr_percentage_becomes_a_fraction(config: dict, nam_api) -> None:  # noqa: ARG001
    """CTR arrives as "1.05%" and is converted to match ad_stats.ctr."""
    stream = TapNextdoor(config=config, parse_env_config=False).streams[
        "ad_performance_reports"
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
    assert stats["end_time"] == "2025-01-31"
