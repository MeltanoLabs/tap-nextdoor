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
                        + ". Defaults to the eleven delivery metrics "
                        "confirmed against a live account: "
                        + ", ".join(streams.REPORT_DEFAULT_METRICS)
                        + "."
                    ),
                    default=list(streams.REPORT_DEFAULT_METRICS),
                ),
                th.Property(
                    "dimensions",
                    th.ArrayType(th.StringType),
                    description=(
                        "Dimensions to break the report down by, including "
                        "the time bucket - DAY, WEEK and MONTH are dimensions "
                        "here, not a separate setting. Supported: "
                        + ", ".join(streams.REPORT_DIMENSIONS)
                        + ". Defaults to DAY, AD_ID, AD."
                    ),
                    default=["DAY", "AD_ID", "AD"],
                ),
                th.Property(
                    "type",
                    th.StringType,
                    description=(
                        "Report category. Supported: "
                        + ", ".join(streams.REPORT_TYPES)
                        + ". Defaults to DELIVERY_METRICS_REPORT."
                    ),
                    default="DELIVERY_METRICS_REPORT",
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
                        "granularity, e.g. creative_performance_report."
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
                    "filters",
                    th.ArrayType(
                        th.ObjectType(
                            th.Property(
                                "attribute",
                                th.StringType,
                                required=True,
                            ),
                            th.Property(
                                "operator",
                                th.StringType,
                                default="CONTAINS",
                            ),
                            th.Property("options", th.ArrayType(th.StringType)),
                        )
                    ),
                    description=(
                        "Restrict the report by entity name, e.g. "
                        '{"attribute": "CAMPAIGN", "operator": "CONTAINS", '
                        '"options": ["Brand"]}. This is the endpoint\'s only '
                        "filtering mechanism and it matches on names; there is "
                        "no documented way to filter by ID."
                    ),
                ),
                th.Property(
                    "window_days",
                    th.IntegerType,
                    description=(
                        "Split the reporting window into slices of this many "
                        "days, building one report per slice. Generation time "
                        "grows with the window and the number of dimensions, "
                        "so a year at a fine grain asked for in one report may "
                        "never finish; 31 is a good starting point. Requires a "
                        "time bucket (DAY/WEEK/MONTH) in dimensions, since "
                        "otherwise every slice emits the same primary key. "
                        "Unset means one report for the whole window."
                    ),
                ),
                th.Property(
                    "poll_interval_seconds",
                    th.IntegerType,
                    description=(
                        "Seconds between status checks while the report is "
                        "generating. Defaults to 5."
                    ),
                    default=5,
                ),
                th.Property(
                    "max_poll_seconds",
                    th.IntegerType,
                    description=(
                        "How long to wait for the report to reach COMPLETED "
                        "before failing the sync. Generation is asynchronous "
                        "and a wide window at a fine grain can take many "
                        "minutes. Defaults to 1800 (30 minutes)."
                    ),
                    default=1800,
                ),
            ),
            title="Ad Performance Report",
            description=(
                "Definition of the custom report built by the "
                "performance_report stream via POST "
                "/api/v3/advertisers/{advertiserId}/reports."
            ),
        ),
        th.Property(
            "lookback_days",
            th.IntegerType,
            title="Lookback Days",
            description=(
                "How far before the bookmark the ad_stats and "
                "performance_report streams restart on an incremental run. Ad "
                "metrics are restated as conversions are attributed after the "
                "fact, so recent days are re-fetched. Defaults to 7."
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
