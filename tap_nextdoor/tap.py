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
    streams.AdPerformanceReportStream,
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
            th.DateType,
            title="Start Date",
            description=(
                "Start of the reporting window for the "
                "ad_performance_reports stream (a LocalDate, e.g. 2024-01-01). "
                "Defaults to today."
            ),
        ),
        th.Property(
            "end_date",
            th.DateType,
            title="End Date",
            description=(
                "End of the reporting window for the ad_performance_reports "
                "stream, inclusive. Defaults to today."
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
                    description="Name given to the generated report.",
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
                "ad_performance_reports stream via POST /reporting/create."
            ),
        ),
        th.Property(
            "page_size",
            th.IntegerType,
            title="Page Size",
            description=(
                "Number of records to request per page from the list "
                "endpoints (pagination_parameters.page_size)."
            ),
            default=100,
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
