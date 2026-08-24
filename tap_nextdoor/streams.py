"""Stream type classes for tap-nextdoor.

Schemas and paths here come from the Nextdoor Ads API reference:
https://developer.nextdoor.com/reference/advertising-introduction

Stream hierarchy (the API has no advertiser *list* endpoint - the set of
advertisers is discovered from ``/me``):

    users        GET /me
    profiles     GET /me
    advertisers  GET /me            -> user.advertisers_with_access[*]
      campaigns          GET /advertiser/campaign/list
        ad_groups        GET /adgroup/list
          ads            GET /ad/list
            ad_performance_reports  GET /ad/get/{id}/stats
          custom_audiences        GET /custom_audience/get/{id}
      creatives          GET /advertiser/creative/list
      reports            GET /advertiser/reportinglist
"""

from __future__ import annotations

import csv
import io
import re
import typing as t

import requests
from singer_sdk import typing as th
from singer_sdk.pagination import SinglePagePaginator

from tap_nextdoor.client import NextdoorStream

if t.TYPE_CHECKING:
    from singer_sdk import Tap
    from singer_sdk.helpers.types import Context


class MeStream(NextdoorStream):
    """Base for the two streams served by ``GET /me``.

    ``/me`` takes no parameters and is not paginated, so the body/pagination
    handling in the base class is switched off.
    """

    path = "/me"
    primary_keys = ("id",)

    def get_new_paginator(self) -> SinglePagePaginator:
        """Return a single-page paginator - ``/me`` returns one object."""
        return SinglePagePaginator()

    def prepare_request_payload(
        self,
        context: Context | None,  # noqa: ARG002
        next_page_token: str | None,  # noqa: ARG002
    ) -> dict | None:
        """Return no request body - ``/me`` takes no parameters."""
        return None


class UserStream(MeStream):
    """The user that owns the access token."""

    name = "users"
    records_jsonpath = "$.user"
    schema = th.PropertiesList(
        th.Property(
            "id", th.StringType, required=True, description="The user's ads ID"
        ),
        th.Property("name", th.StringType, description="User's display name"),
        th.Property("email", th.StringType, description="User's email address"),
        th.Property(
            "email_confirmed",
            th.BooleanType,
            description="Whether the user has confirmed their email",
        ),
        th.Property(
            "advertisers_with_access",
            description="Advertisers this user can access, with their role on each",
            wrapped=th.ArrayType(
                th.ObjectType(
                    th.Property("advertiser_id", th.StringType),
                    th.Property("role", th.StringType),
                ),
            ),
        ),
    ).to_dict()


class ProfileStream(MeStream):
    """The advertising profile associated with the access token."""

    name = "profiles"
    records_jsonpath = "$.profile"
    schema = th.PropertiesList(
        th.Property(
            "id", th.StringType, required=True, description="The profile's ads ID"
        ),
        th.Property("name", th.StringType, description="Profile name"),
        th.Property(
            "associated_user_ids",
            th.ArrayType(th.StringType),
            description="Users attached to this profile",
        ),
        th.Property(
            "payment_profile_id",
            th.StringType,
            description="Billing profile backing this advertising profile",
        ),
        th.Property(
            "is_ad_agency",
            th.BooleanType,
            description="Whether the profile represents an agency",
        ),
    ).to_dict()


class AdvertiserStream(MeStream):
    """Advertisers the access token has access to.

    The Ads API exposes ``advertiser/create`` and ``advertiser/get/{id}/stats``
    but no advertiser list endpoint, so the accessible advertisers are read out
    of ``/me``'s ``user.advertisers_with_access`` and optionally narrowed by the
    ``advertiser_ids`` config setting.
    """

    name = "advertisers"
    records_jsonpath = "$.user.advertisers_with_access[*]"
    primary_keys = ("advertiser_id",)
    schema = th.PropertiesList(
        th.Property(
            "advertiser_id",
            th.StringType,
            required=True,
            description=(
                "Advertiser ID. Sourced from `id` in the API response, which "
                "the reference docs call advertiser_id."
            ),
        ),
        th.Property(
            "role",
            th.StringType,
            description="The token holder's role on this advertiser, e.g. CLIENT_ADMIN",
        ),
    ).to_dict()

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Normalise the id field and apply the ``advertiser_ids`` filter.

        The reference docs describe these entries as ``advertiser_id``, but the
        live API returns ``id``; both are accepted here.
        """
        row = super().post_process(row, context) or row
        row["advertiser_id"] = row.pop("id", row.get("advertiser_id"))
        selected = self.config.get("advertiser_ids")
        if selected and row["advertiser_id"] not in selected:
            return None
        return row

    def get_child_context(self, record: dict, context: Context | None) -> dict:  # noqa: ARG002
        """Pass the advertiser id down to child streams."""
        return {"advertiser_id": record["advertiser_id"]}


class CampaignStream(NextdoorStream):
    """Campaigns belonging to an advertiser."""

    name = "campaigns"
    path = "/advertiser/campaign/list"
    primary_keys = ("id",)
    replication_key = "updated_at"
    records_jsonpath = "$.campaigns[*].data"
    parent_stream_type = AdvertiserStream

    schema = th.PropertiesList(
        th.Property("id", th.StringType, required=True, description="Campaign ID"),
        th.Property(
            "advertiser_id",
            th.StringType,
            description="ID of the advertiser that owns the campaign",
        ),
        th.Property("name", th.StringType, description="Campaign name"),
        th.Property(
            "status",
            th.StringType,
            description=(
                "Effective delivery status, e.g. ACTIVE, PAUSED, ARCHIVED. "
                "May differ from user_status when a parent is paused."
            ),
        ),
        th.Property(
            "user_status",
            th.StringType,
            description="Status explicitly set by the advertiser",
        ),
        th.Property(
            "objective",
            th.StringType,
            description="Campaign objective, e.g. CONVERSION, TRAFFIC",
        ),
        # Returned live but absent from the reference docs.
        th.Property(
            "sub_objective",
            th.StringType,
            description=(
                "Objective refinement, e.g. WEBSITE_CONVERSIONS. Undocumented."
            ),
        ),
        th.Property(
            "special_ad_category",
            th.BooleanType,
            description=(
                "Whether the campaign is in a regulated category (housing, "
                "credit, employment). Undocumented."
            ),
        ),
        th.Property(
            "created_at", th.DateTimeType, description="When the campaign was created"
        ),
        th.Property(
            "updated_at",
            th.DateTimeType,
            description="When the campaign was last modified; replication key",
        ),
        th.Property(
            "start_time",
            th.DateTimeType,
            description="Scheduled start of delivery",
        ),
        th.Property(
            "end_time",
            th.DateTimeType,
            description="Scheduled end of delivery; absent if open-ended",
        ),
    ).to_dict()

    def get_child_context(self, record: dict, context: Context | None) -> dict:
        """Pass the campaign and advertiser ids down to child streams."""
        return {
            "advertiser_id": (context or {})["advertiser_id"],
            "campaign_id": record["id"],
        }


class AdGroupStream(NextdoorStream):
    """Ad groups belonging to a campaign."""

    name = "ad_groups"
    path = "/adgroup/list"
    primary_keys = ("id",)
    replication_key = "updated_at"
    records_jsonpath = "$.adgroups[*].data"
    parent_stream_type = CampaignStream

    _time_window = th.ObjectType(
        th.Property("start_time", th.StringType),
        th.Property("end_time", th.StringType),
    )
    _day_part = th.ArrayType(
        th.ObjectType(
            th.Property("days", th.ArrayType(th.StringType)),
            th.Property("time", _time_window),
        ),
    )
    # include/exclude are *nested* lists - each inner list is a targeting
    # group, e.g. {"include": [["649156285417129166"]], "exclude": []}.
    _include_exclude = th.ObjectType(
        th.Property("include", th.ArrayType(th.ArrayType(th.StringType))),
        th.Property("exclude", th.ArrayType(th.ArrayType(th.StringType))),
    )

    schema = th.PropertiesList(
        th.Property("id", th.StringType, required=True, description="Ad group ID"),
        th.Property("advertiser_id", th.StringType, description="Owning advertiser ID"),
        th.Property("campaign_id", th.StringType, description="Parent campaign ID"),
        th.Property("name", th.StringType, description="Ad group name"),
        th.Property(
            "status",
            th.StringType,
            description=(
                "Effective delivery status, e.g. ACTIVE, PAUSED_DUE_TO_CAMPAIGN_PAUSE"
            ),
        ),
        th.Property(
            "user_status",
            th.StringType,
            description="Status explicitly set by the advertiser",
        ),
        th.Property(
            "placements",
            th.ArrayType(th.StringType),
            description="Where ads may serve, e.g. FEED, FSF, RHR",
        ),
        th.Property(
            "audience_network_is_on",
            th.BooleanType,
            description="Whether off-Nextdoor audience network delivery is enabled",
        ),
        th.Property(
            "bid",
            description="Bid settings for the ad group",
            wrapped=th.ObjectType(
                # Money is returned as a currency-prefixed string, e.g. "GBP 3.35".
                th.Property("amount", th.StringType),
                th.Property("pricing_type", th.StringType),
                th.Property("bid_strategy", th.StringType),
            ),
        ),
        th.Property(
            "budget",
            description="Budget settings for the ad group",
            wrapped=th.ObjectType(
                th.Property("amount", th.StringType),
                th.Property("budget_type", th.StringType),
                th.Property("lifetime_delivery_cap_type", th.StringType),
            ),
        ),
        th.Property(
            "start_time", th.DateTimeType, description="Scheduled start of delivery"
        ),
        th.Property(
            "end_time",
            th.DateTimeType,
            description="Scheduled end of delivery; absent if open-ended",
        ),
        th.Property(
            "frequency_caps",
            description="Limits on how often one neighbour sees these ads",
            wrapped=th.ArrayType(
                th.ObjectType(
                    th.Property("max_impressions", th.StringType),
                    th.Property("num_timeunits", th.StringType),
                    th.Property("timeunit", th.StringType),
                ),
            ),
        ),
        th.Property(
            "targeting",
            description="Geographic, audience, interest and daypart targeting",
            wrapped=th.ObjectType(
                th.Property(
                    "included_location_targeting_ids", th.ArrayType(th.StringType)
                ),
                th.Property(
                    "excluded_location_targeting_ids", th.ArrayType(th.StringType)
                ),
                th.Property("audience_targeting_ids", th.ArrayType(th.StringType)),
                # Live shape: custom audiences and interests are include/exclude
                # objects here, not the documented top-level custom_audience_ids.
                th.Property("custom_audience_targeting", _include_exclude),
                th.Property("interests_targeting", _include_exclude),
                th.Property("geo_source_types", th.ArrayType(th.StringType)),
                th.Property(
                    "time_of_day",
                    th.ObjectType(
                        th.Property("included", _day_part),
                        th.Property("excluded", _day_part),
                    ),
                ),
            ),
        ),
        th.Property(
            "custom_audience_ids",
            th.ArrayType(th.StringType),
            description=(
                "Custom audience IDs. Documented as a top-level field but not "
                "returned by the live API, which nests them under "
                "targeting.custom_audience_targeting; both are read."
            ),
        ),
        th.Property("created_at", th.DateTimeType, description="When it was created"),
        th.Property(
            "updated_at",
            th.DateTimeType,
            description="When it was last modified; replication key",
        ),
    ).to_dict()

    def get_body_params(self, context: Context | None) -> dict[str, t.Any]:
        """Add the required ``campaign_id`` to the request body."""
        context = context or {}
        return {
            "advertiser_id": context["advertiser_id"],
            "campaign_id": context["campaign_id"],
        }

    def get_child_context(self, record: dict, context: Context | None) -> dict:
        """Pass ids (and any custom audience ids) down to child streams."""
        return {
            "advertiser_id": (context or {})["advertiser_id"],
            "adgroup_id": record["id"],
            "custom_audience_ids": self._audience_ids(record),
        }

    @classmethod
    def _audience_ids(cls, record: dict) -> list[str]:
        """Collect custom audience ids from an ad group's targeting.

        The docs describe a top-level ``custom_audience_ids`` array, but the
        live API nests them under
        ``targeting.custom_audience_targeting.{include,exclude}`` as *lists of
        targeting groups* - e.g. ``{"include": [["6491562854171"]]}``. Both
        shapes are handled, and ids are de-duplicated in first-seen order.
        """
        if documented := record.get("custom_audience_ids"):
            return list(dict.fromkeys(documented))

        targeting = record.get("targeting") or {}
        audiences = targeting.get("custom_audience_targeting") or {}
        ids: list[str] = []
        for key in ("include", "exclude"):
            ids.extend(cls._flatten_ids(audiences.get(key) or []))
        return list(dict.fromkeys(ids))

    @classmethod
    def _flatten_ids(cls, entries: object) -> list[str]:
        """Flatten nested targeting groups into a flat list of id strings."""
        if isinstance(entries, str):
            return [entries]
        if isinstance(entries, dict):
            found = entries.get("id") or entries.get("audience_id")
            return [found] if found else []
        if isinstance(entries, (list, tuple)):
            return [id_ for entry in entries for id_ in cls._flatten_ids(entry)]
        return []


class AdStream(NextdoorStream):
    """Ads belonging to an ad group."""

    name = "ads"
    path = "/ad/list"
    primary_keys = ("id",)
    replication_key = "updated_at"
    records_jsonpath = "$.ads[*].data"
    parent_stream_type = AdGroupStream
    # The parent context also carries custom_audience_ids for the audience
    # stream; keep that list out of this stream's state partition keys.
    state_partitioning_keys = ("advertiser_id", "adgroup_id")

    schema = th.PropertiesList(
        th.Property("id", th.StringType, required=True, description="Ad ID"),
        th.Property("advertiser_id", th.StringType, description="Owning advertiser ID"),
        th.Property("adgroup_id", th.StringType, description="Parent ad group ID"),
        th.Property(
            "creative_id",
            th.StringType,
            description="Creative rendered by this ad; joins to the creatives stream",
        ),
        th.Property("name", th.StringType, description="Ad name"),
        th.Property(
            "status",
            th.StringType,
            description=(
                "Effective delivery status, e.g. ACTIVE, INACTIVE, ARCHIVED, INELIGIBLE"
            ),
        ),
        th.Property(
            "user_status",
            th.StringType,
            description="Status explicitly set by the advertiser",
        ),
        th.Property(
            "created_at", th.DateTimeType, description="When the ad was created"
        ),
        th.Property(
            "updated_at",
            th.DateTimeType,
            description="When the ad was last modified; replication key",
        ),
    ).to_dict()

    def get_body_params(self, context: Context | None) -> dict[str, t.Any]:
        """Add the required ``adgroup_id`` to the request body."""
        context = context or {}
        return {
            "advertiser_id": context["advertiser_id"],
            "adgroup_id": context["adgroup_id"],
        }

    def get_child_context(self, record: dict, context: Context | None) -> dict:
        """Pass the ad id down to the performance stream."""
        return {
            "advertiser_id": (context or {})["advertiser_id"],
            "ad_id": record["id"],
        }


class CreativeStream(NextdoorStream):
    """Creatives belonging to an advertiser."""

    name = "creatives"
    path = "/advertiser/creative/list"
    primary_keys = ("id",)
    replication_key = "updated_at"
    records_jsonpath = "$.creatives[*].data"
    parent_stream_type = AdvertiserStream

    schema = th.PropertiesList(
        th.Property("id", th.StringType, required=True, description="Creative ID"),
        th.Property("advertiser_id", th.StringType, description="Owning advertiser ID"),
        th.Property("name", th.StringType, description="Creative name"),
        th.Property(
            "status",
            th.StringType,
            description="Review status, e.g. APPROVED",
        ),
        th.Property(
            "placement", th.StringType, description="Placement this creative targets"
        ),
        # Returned live but absent from the reference docs.
        th.Property(
            "creative_type",
            th.StringType,
            description=("Creative format, e.g. IMAGE_NATIVE_V3. Undocumented."),
        ),
        th.Property(
            "text_overlays",
            th.ArrayType(th.StringType),
            description="Text rendered over the image. Undocumented.",
        ),
        th.Property(
            "advertiser_name",
            th.StringType,
            description="Advertiser name shown to neighbours",
        ),
        th.Property("headline", th.StringType, description="Headline text"),
        th.Property("body_text", th.StringType, description="Body copy"),
        th.Property("offer_text", th.StringType, description="Offer text, if any"),
        th.Property("price", th.StringType, description="Displayed price, if any"),
        th.Property(
            "cta", th.StringType, description="Call to action, e.g. Learn more"
        ),
        th.Property(
            "canvas_image_url", th.StringType, description="Main creative image"
        ),
        th.Property("logo_image_url", th.StringType, description="Advertiser logo"),
        th.Property("clickthrough_url", th.StringType, description="Destination URL"),
        th.Property(
            "impression_tracking_urls",
            th.ArrayType(th.StringType),
            description="Third-party impression pixels",
        ),
        th.Property(
            "click_tracking_urls",
            th.ArrayType(th.StringType),
            description="Third-party click trackers",
        ),
        th.Property("created_at", th.DateTimeType, description="When it was created"),
        th.Property(
            "updated_at",
            th.DateTimeType,
            description="When it was last modified; replication key",
        ),
    ).to_dict()


class ReportStream(NextdoorStream):
    """Saved/scheduled reports for an advertiser.

    This lists report *definitions* and their ``download_url`` - it does not
    contain metrics. Metrics come from ``AdPerformanceStream`` below.
    """

    name = "reports"
    # The reference docs give /advertiser/reportinglist, which 404s;
    # /advertiser/reporting/list is the working path.
    path = "/advertiser/reporting/list"
    primary_keys = ("id",)
    records_jsonpath = "$.reports[*].data"
    parent_stream_type = AdvertiserStream

    schema = th.PropertiesList(
        th.Property("id", th.StringType, required=True, description="Report ID"),
        th.Property("advertiser_id", th.StringType, description="Owning advertiser ID"),
        th.Property("name", th.StringType, description="Report name"),
        th.Property(
            "download_url",
            th.StringType,
            description=(
                "Presigned S3 URL for the report CSV. Short-lived, and carries "
                "embedded AWS credentials - treat as a secret."
            ),
        ),
    ).to_dict()


class AdStatsStream(NextdoorStream):
    """Per-ad aggregate metrics from ``GET /ad/get/{id}/stats``.

    A cheap-to-reason-about alternative to ``ad_performance_reports``: it has
    no side effects, but returns one aggregate row per ad for the window
    rather than a daily time series, so it is queried once per ad for
    ``start_date`` -> ``end_date``.

    The OpenAPI definition declares an empty response object, so this schema
    was built from a live response. Two things differ from the endpoint's prose
    description: ``start_time``/``end_time`` come back as full timestamps
    (``2026-06-30T23:00:00Z``) rather than LocalDates, and every money field is
    a currency-prefixed string (``"GBP 0"``) rather than a number. Additional
    properties are allowed so new metrics are not dropped.
    """

    name = "ad_stats"
    path = "/ad/get/{ad_id}/stats"
    primary_keys = ("ad_id", "start_time", "end_time")
    records_jsonpath = "$"
    parent_stream_type = AdStream
    state_partitioning_keys: t.ClassVar[list[str]] = []
    zoned_datetime_fields = ()

    schema = th.PropertiesList(
        th.Property(
            "ad_id",
            th.StringType,
            required=True,
            description="Ad these metrics are for",
        ),
        th.Property("advertiser_id", th.StringType, description="Owning advertiser ID"),
        th.Property(
            "start_time", th.DateTimeType, description="Start of the reporting window"
        ),
        th.Property(
            "end_time",
            th.DateTimeType,
            description="End of the reporting window, inclusive",
        ),
        # Money fields are currency-prefixed strings, e.g. "GBP 12.50".
        th.Property(
            "billable_spend",
            th.StringType,
            description='Billable spend, currency-prefixed, e.g. "GBP 12.50"',
        ),
        th.Property(
            "cpc", th.StringType, description="Cost per click, currency-prefixed"
        ),
        th.Property(
            "cpm",
            th.StringType,
            description="Cost per thousand impressions, currency-prefixed",
        ),
        th.Property(
            "cost_per_result",
            th.StringType,
            description="Cost per result, currency-prefixed",
        ),
        th.Property("impressions", th.IntegerType, description="Impressions served"),
        th.Property("clicks", th.IntegerType, description="Clicks received"),
        th.Property(
            "ctr", th.NumberType, description="Click-through rate as a fraction"
        ),
        th.Property(
            "result",
            th.NumberType,
            description="Results against the campaign objective",
        ),
        th.Property(
            "total_conversions",
            th.IntegerType,
            description="All conversions, summing the breakdown below",
        ),
        # Conversion breakdown, returned live but undocumented. Each counts
        # conversions of that Nextdoor pixel event type.
        th.Property(
            "purchase_conversions", th.IntegerType, description="Purchase conversions"
        ),
        th.Property("lead_conversions", th.IntegerType, description="Lead conversions"),
        th.Property(
            "sign_up_conversions", th.IntegerType, description="Sign-up conversions"
        ),
        th.Property(
            "add_to_cart_conversions",
            th.IntegerType,
            description="Add-to-cart conversions",
        ),
        th.Property(
            "initiate_checkout_conversions",
            th.IntegerType,
            description="Checkout-initiated conversions",
        ),
        th.Property(
            "search_conversions", th.IntegerType, description="Search conversions"
        ),
        th.Property(
            "view_content_conversions",
            th.IntegerType,
            description="View-content conversions",
        ),
        th.Property(
            "add_to_wishlist_conversions",
            th.IntegerType,
            description="Add-to-wishlist conversions",
        ),
        th.Property(
            "subscribe_conversions",
            th.IntegerType,
            description="Subscribe conversions",
        ),
        th.Property(
            "other_conversions",
            th.IntegerType,
            description="Conversions not in the categories above",
        ),
        additional_properties=True,
    ).to_dict()

    def get_new_paginator(self) -> SinglePagePaginator:
        """Return a single-page paginator - the stats endpoint is not paginated."""
        return SinglePagePaginator()

    def prepare_request_payload(
        self,
        context: Context | None,
        next_page_token: str | None,  # noqa: ARG002
    ) -> dict | None:
        """Send the advertiser id and the reporting window in the body.

        ``start_time``/``end_time`` are ``LocalDate`` values ("2024-01-01") and
        ``end_time`` is inclusive.
        """
        context = context or {}
        return {
            "advertiser_id": context["advertiser_id"],
            "start_time": self.window_date("start_date").isoformat(),
            "end_time": self.window_date("end_date").isoformat(),
        }

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Stamp the ad and advertiser ids onto the metrics row."""
        context = context or {}
        row["ad_id"] = context["ad_id"]
        row["advertiser_id"] = context["advertiser_id"]
        return row


class CustomAudienceStream(NextdoorStream):
    """Custom audiences referenced by ad groups.

    The API has ``custom_audience`` create/get/archive endpoints but no list
    endpoint, so audiences are fetched by the ids found on their ad groups.
    """

    name = "custom_audiences"
    path = "/custom_audience/get/{custom_audience_id}"
    primary_keys = ("id",)
    replication_key = "updated_at"
    records_jsonpath = "$"
    parent_stream_type = AdGroupStream
    state_partitioning_keys = ("advertiser_id", "adgroup_id")

    schema = th.PropertiesList(
        th.Property(
            "id", th.StringType, required=True, description="Custom audience ID"
        ),
        th.Property("advertiser_id", th.StringType, description="Owning advertiser ID"),
        th.Property(
            "adgroup_id",
            th.StringType,
            description="The ad group this audience was discovered from",
        ),
        th.Property("name", th.StringType, description="Audience name"),
        th.Property("description", th.StringType, description="Advertiser's own notes"),
        th.Property(
            "audience_type",
            th.StringType,
            description="How the audience was built, e.g. emails",
        ),
        th.Property("created_at", th.DateTimeType, description="When it was created"),
        th.Property(
            "updated_at",
            th.DateTimeType,
            description="When it was last modified; replication key",
        ),
    ).to_dict()

    def get_new_paginator(self) -> SinglePagePaginator:
        """Return a single-page paginator - get-by-id is not paginated."""
        return SinglePagePaginator()

    def prepare_request_payload(
        self,
        context: Context | None,  # noqa: ARG002
        next_page_token: str | None,  # noqa: ARG002
    ) -> dict | None:
        """Return no request body - the id is a path parameter."""
        return None

    def __init__(self, *args, **kwargs) -> None:
        """Track audiences already fetched, since they are shared across ad groups."""
        super().__init__(*args, **kwargs)
        self._seen_ids: set[str] = set()

    def get_records(self, context: Context | None) -> t.Iterable[dict]:
        """Fetch each custom audience referenced by the parent ad group.

        There is no list endpoint, so this issues one get-by-id request per
        distinct audience id found on the ad groups.
        """
        for audience_id in (context or {}).get("custom_audience_ids") or []:
            if audience_id in self._seen_ids:
                continue
            self._seen_ids.add(audience_id)
            yield from super().get_records(
                {**(context or {}), "custom_audience_id": audience_id},
            )

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Drop the parent's id list, which the SDK merges in from the context."""
        row = super().post_process(row, context) or row
        row.pop("custom_audience_ids", None)
        return row


# Documented enums for POST /reporting/create. Config values are validated
# against these so a typo fails with a clear message instead of a 400.
REPORT_METRICS = (
    "IMPRESSIONS",
    "CLICKS",
    "CTR",
    "SPEND",
    "BILLABLE_SPEND",
    "CPM",
    "CPC",
    "CONVERSIONS",
)
REPORT_DIMENSIONS = ("CAMPAIGN", "AD_GROUP", "AD", "PLACEMENT")
REPORT_TIME_GRANULARITIES = ("DAY", "WEEK", "MONTH")

#: Metrics returned as currency-prefixed strings ("GBP 12.50") rather than numbers.
_MONEY_METRICS = frozenset({"SPEND", "BILLABLE_SPEND", "CPM", "CPC"})
#: Metrics returned as whole numbers.
_INTEGER_METRICS = frozenset({"IMPRESSIONS", "CLICKS", "CONVERSIONS"})

#: JSON type per metric; anything unlisted is treated as a number.
_METRIC_TYPES: dict[str, t.Any] = {
    **dict.fromkeys(_MONEY_METRICS, th.StringType),
    **dict.fromkeys(_INTEGER_METRICS, th.IntegerType),
}

#: Column added to the report for each requested dimension granularity,
#: with the description attached to it in the generated schema.
_DIMENSION_COLUMNS = {
    "CAMPAIGN": (
        ("campaign_id", "Campaign ID; joins to the campaigns stream"),
        ("campaign_name", "Campaign name"),
    ),
    "AD_GROUP": (
        ("adgroup_id", "Ad group ID; joins to the ad_groups stream"),
        ("adgroup_name", "Ad group name"),
    ),
    "AD": (
        ("ad_id", "Ad ID; joins to the ads stream"),
        ("ad_name", "Ad name"),
    ),
    "PLACEMENT": (("placement", "Placement the metrics are attributed to"),),
}

#: Description per report metric.
_METRIC_DESCRIPTIONS = {
    "IMPRESSIONS": "Impressions served",
    "CLICKS": "Clicks received",
    "CTR": "Click-through rate as a fraction",
    "SPEND": 'Spend, currency-prefixed, e.g. "GBP 12.50"',
    "BILLABLE_SPEND": "Billable spend, currency-prefixed",
    "CPM": "Cost per thousand impressions, currency-prefixed",
    "CPC": "Cost per click, currency-prefixed",
    "CONVERSIONS": "Conversions attributed in the window",
}
#: Id column per dimension, used to build the primary key.
_DIMENSION_KEYS = {
    "CAMPAIGN": "campaign_id",
    "AD_GROUP": "adgroup_id",
    "AD": "ad_id",
    "PLACEMENT": "placement",
}


class AdPerformanceReportStream(NextdoorStream):
    """A custom ad performance report, defined entirely by the ``report`` config.

    Unlike every other stream here this one **writes**: ``POST /reporting/create``
    generates an ad hoc report, emails it to ``recipient_emails``, and returns a
    presigned ``download_url`` for the CSV, which this stream then downloads and
    emits row by row. Two consequences worth knowing:

    * Each sync creates a new report object in the advertiser's account. They
      accumulate - the ``reports`` stream lists everything created so far.
    * Each sync emails the recipients. Leave ``recipient_emails`` empty to skip
      the email while still generating the report, if the API allows it (the
      OpenAPI schema does not mark the field required).

    The CSV's column headers are normalised to snake_case (``"Ad ID"`` ->
    ``ad_id``). Because the header row is not documented, the schema allows
    additional properties so unexpected columns are passed through rather than
    dropped.
    """

    name = "ad_performance_reports"
    path = "/reporting/create"
    http_method = "POST"
    records_jsonpath = "$"  # unused; parse_response is overridden
    parent_stream_type = AdvertiserStream
    zoned_datetime_fields = ()

    def __init__(self, tap: Tap, **kwargs: t.Any) -> None:
        """Build the schema and key from the ``report`` config block.

        Args:
            tap: The parent tap.
            kwargs: Additional stream arguments.
        """
        self._report = self._validated_report(tap.config.get("report") or {})
        super().__init__(tap=tap, schema=self._build_schema(self._report), **kwargs)
        keys = [_DIMENSION_KEYS[d] for d in self._report["dimension_granularity"]]
        self._primary_keys = ("advertiser_id", "date", *keys)

    @property
    def report_config(self) -> dict[str, t.Any]:
        """Return the validated ``report`` config block, with defaults applied."""
        return self._report

    @staticmethod
    def _validated_report(configured: dict[str, t.Any]) -> dict[str, t.Any]:
        """Validate the configured report definition and apply defaults."""
        metrics = list(configured.get("metrics") or REPORT_METRICS)
        dimensions = list(configured.get("dimension_granularity") or ["AD"])
        time_granularity = list(configured.get("time_granularity") or ["DAY"])

        for values, allowed, label in (
            (metrics, REPORT_METRICS, "metrics"),
            (dimensions, REPORT_DIMENSIONS, "dimension_granularity"),
            (time_granularity, REPORT_TIME_GRANULARITIES, "time_granularity"),
        ):
            if invalid := [v for v in values if v not in allowed]:
                msg = (
                    f"Invalid report.{label} value(s) {invalid}. "
                    f"Supported values: {list(allowed)}"
                )
                raise ValueError(msg)

        return {
            "metrics": metrics,
            "dimension_granularity": dimensions,
            "time_granularity": time_granularity,
            "name": configured.get("name") or "tap-nextdoor ad performance",
            "recipient_emails": list(configured.get("recipient_emails") or []),
            "campaign_ids": list(configured.get("campaign_ids") or []),
            "adgroup_ids": list(configured.get("adgroup_ids") or []),
            "ad_ids": list(configured.get("ad_ids") or []),
        }

    @staticmethod
    def _build_schema(report: dict[str, t.Any]) -> dict:
        """Build the schema from the requested dimensions and metrics."""
        properties = [
            th.Property(
                "advertiser_id",
                th.StringType,
                required=True,
                description="Advertiser the report was generated for",
            ),
            th.Property(
                "report_id",
                th.StringType,
                description="ID of the generated report; joins to the reports stream",
            ),
            th.Property(
                "date",
                th.StringType,
                description=(
                    "The report time bucket, at the configured time_granularity"
                ),
            ),
        ]

        for dimension in report["dimension_granularity"]:
            properties.extend(
                th.Property(column, th.StringType, description=description)
                for column, description in _DIMENSION_COLUMNS[dimension]
            )

        for metric in report["metrics"]:
            # Money comes back currency-prefixed ("GBP 12.50"), so it stays a
            # string; CTR and any future ratio metric is a float.
            metric_type: t.Any = _METRIC_TYPES.get(metric, th.NumberType)
            properties.append(
                th.Property(
                    metric.lower(),
                    metric_type,
                    description=_METRIC_DESCRIPTIONS.get(metric),
                )
            )

        return th.PropertiesList(*properties, additional_properties=True).to_dict()

    def prepare_request_payload(
        self,
        context: Context | None,
        next_page_token: str | None,  # noqa: ARG002
    ) -> dict | None:
        """Build the report definition sent to ``POST /reporting/create``."""
        report = self.report_config
        payload = {
            "advertiser_id": (context or {})["advertiser_id"],
            "name": report["name"],
            "recipient_emails": report["recipient_emails"],
            "dimension_granularity": report["dimension_granularity"],
            "time_granularity": report["time_granularity"],
            "metrics": report["metrics"],
            "start_time": self.window_date("start_date").isoformat(),
            "end_time": self.window_date("end_date").isoformat(),
        }
        for key in ("campaign_ids", "adgroup_ids", "ad_ids"):
            if report[key]:
                payload[key] = report[key]
        return payload

    def get_new_paginator(self) -> SinglePagePaginator:
        """Return a single-page paginator - one report per request."""
        return SinglePagePaginator()

    def parse_response(self, response: requests.Response) -> t.Iterable[dict]:
        """Download the generated CSV and yield one record per row.

        Args:
            response: The ``reporting/create`` response, carrying ``download_url``.

        Yields:
            One record per CSV data row.
        """
        created = response.json()
        download_url = created.get("download_url")
        if not download_url:
            self.logger.warning(
                "No download_url returned for report %s; nothing to emit.",
                created.get("id"),
            )
            return

        # The URL is presigned, so it must be fetched without the tap's
        # Authorization header - S3 rejects requests carrying two auth methods.
        csv_response = requests.get(download_url, timeout=self.timeout)
        csv_response.raise_for_status()

        reader = csv.DictReader(io.StringIO(csv_response.text))
        for row in reader:
            record = {
                self._normalise_header(key): value
                for key, value in row.items()
                if key is not None
            }
            record["report_id"] = created.get("id")
            yield record

    @staticmethod
    def _normalise_header(header: str) -> str:
        """Normalise a CSV header to snake_case, e.g. ``"Ad ID"`` -> ``ad_id``."""
        return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Stamp the advertiser id on the row and cast numeric metrics."""
        row = super().post_process(row, context) or row
        row["advertiser_id"] = (context or {})["advertiser_id"]

        for metric in self.report_config["metrics"]:
            column = metric.lower()
            value = row.get(column)
            if value in (None, ""):
                continue
            if metric in _INTEGER_METRICS:
                row[column] = int(float(value))
            elif metric not in _MONEY_METRICS:
                row[column] = float(value)
        return row
