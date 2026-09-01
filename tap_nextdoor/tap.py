"""Nextdoor tap class."""

from __future__ import annotations

from singer_sdk import Tap
from singer_sdk import typing as th

from tap_nextdoor import streams

STREAM_TYPES = [
    streams.UserStream,
    streams.ProfileStream,
    streams.AdvertiserStream,
    streams.CampaignStream,
    streams.AdGroupStream,
    streams.AdStream,
    streams.CreativeStream,
    streams.ReportStream,
    streams.AdStatsStream,
    streams.PerformanceReportStream,
    streams.CustomAudienceStream,
]


class TapNextdoor(Tap):
    """Singer tap for the Nextdoor Ads Manager (NAM) API."""

    name = "tap-nextdoor"

    config_jsonschema = th.PropertiesList(
        th.Property(
            "access_token",
            th.StringType,
            required=True,
            title="Access Token",
            description=(
                "Ads API access token, generated in Nextdoor Ads Manager at "
                "https://ads.nextdoor.com/v2/manage/api. Sent as a bearer "
                "token on every request."
            ),
            secret=True,
        ),
        th.Property(
            "advertiser_ids",
            th.ArrayType(th.StringType),
            title="Advertiser IDs",
            description=(
                "Filter advertisers (and their child campaigns/ad groups/ads) "
                "to extract data for by ID. Leave empty to extract all "
                "advertisers the access token has access to, as reported by "
                "the /me endpoint."
            ),
            default=[],
        ),
        th.Property(
            "start_date",
            th.DateTimeType,
            title="Start Date",
            description=(
                "Start of the reporting window for the performance_report "
                "and ad_stats streams. Accepts an ISO-8601 date-time "
                "(2026-01-01T00:00:00Z) or a plain date (2026-01-01); the API "
                "takes whole days, so any time component is truncated. "
                "Defaults to today."
            ),
        ),
        th.Property(
            "end_date",
            th.DateTimeType,
            title="End Date",
            description=(
                "End of the reporting window, inclusive. Accepts an ISO-8601 "
                "date-time (2026-01-31T23:59:59Z) or a plain date "
                "(2026-01-31). Defaults to today."
            ),
        ),
        th.Property(
            "report",
            th.ObjectType(
                th.Property(
                    "metrics",
                    th.ArrayType(th.StringType),
                    description=(
                        "Metrics to include. Supported: "
                        + ", ".join(streams.REPORT_METRICS)
                        + ". Defaults to all of them."
                    ),
                    default=list(streams.REPORT_METRICS),
                ),
                th.Property(
                    "dimension_granularity",
                    th.ArrayType(th.StringType),
                    description=(
                        "Dimensions to break the report down by. Supported: "
                        + ", ".join(streams.REPORT_DIMENSIONS)
                        + ". Defaults to AD."
                    ),
                    default=["AD"],
                ),
                th.Property(
                    "time_granularity",
                    th.ArrayType(th.StringType),
                    description=(
                        "Time bucket for each row. Supported: "
                        + ", ".join(streams.REPORT_TIME_GRANULARITIES)
                        + ". Defaults to DAY."
                    ),
                    default=["DAY"],
                ),
                th.Property(
                    "name",
                    th.StringType,
                    description="Name given to the generated report in NAM.",
                ),
                th.Property(
                    "stream_name",
                    th.StringType,
                    description=(
                        "Override the stream's name. Defaults to "
                        "performance_report; set it to match the chosen "
                        "granularity, e.g. campaign_performance_report."
                    ),
                ),
                th.Property(
                    "recipient_emails",
                    th.ArrayType(th.StringType),
                    description=(
                        "Emails the generated report is sent to. Every sync "
                        "emails these recipients, so leave empty to skip the "
                        "email and only download the CSV."
                    ),
                    default=[],
                ),
                th.Property(
                    "campaign_ids",
                    th.ArrayType(th.StringType),
                    description="Restrict the report to these campaigns.",
                ),
                th.Property(
                    "adgroup_ids",
                    th.ArrayType(th.StringType),
                    description="Restrict the report to these ad groups.",
                ),
                th.Property(
                    "ad_ids",
                    th.ArrayType(th.StringType),
                    description="Restrict the report to these ads.",
                ),
            ),
            title="Ad Performance Report",
            description=(
                "Definition of the custom report built by the "
                "performance_report stream via POST /reporting/create."
            ),
        ),
        th.Property(
            "lookback_days",
            th.IntegerType,
            title="Lookback Days",
            description=(
                "How far before the bookmark the ad_stats stream restarts on "
                "an incremental run. Ad metrics are restated as conversions "
                "are attributed after the fact, so recent days are "
                "re-fetched. Defaults to 7."
            ),
            default=7,
        ),
        th.Property(
            "page_size",
            th.IntegerType,
            title="Page Size",
            description=(
                "Number of records to request per page from the list "
                "endpoints (pagination_parameters.page_size)."
            ),
            default=500,
        ),
    ).to_dict()

    def discover_streams(self) -> list:
        """Return a list of discovered streams.

        Returns:
            A list of streams.
        """
        return [stream_cls(tap=self) for stream_cls in STREAM_TYPES]


if __name__ == "__main__":
    TapNextdoor.cli()
