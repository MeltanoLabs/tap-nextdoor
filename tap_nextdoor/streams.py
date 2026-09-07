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
            ad_stats     GET /ad/get/{id}/stats
          custom_audiences        GET /custom_audience/get/{id}
      creatives          GET /advertiser/creative/list
      reports            GET /advertiser/reporting/list
      performance_report  POST /api/v3/advertisers/{id}/reports,
                          polled, then CSV download
"""

from __future__ import annotations

import csv
import io
import re
import time
import typing as t
from datetime import date, datetime, timedelta, timezone
from functools import cached_property

import requests
from singer_sdk import typing as th
from singer_sdk.pagination import SinglePagePaginator

from tap_nextdoor.client import (
    DEFAULT_LOOKBACK_DAYS,
    NextdoorStream,
    as_local_date,
)

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


class AdvertiserStream(NextdoorStream):
    """Advertisers the access token has access to, with their full detail.

    Two endpoints are combined. ``/me`` is the only way to discover *which*
    advertisers a token can reach, and it reports just an id and the token
    holder's role. The detail - name, currency, timezone, billing - comes from
    ``GET /advertiser/get/{id}``, which is undocumented: it appears in neither
    the reference nor ``llms.txt``, and was found by trying the ``get/{id}``
    shape that campaigns and custom audiences use. Being undocumented it may
    change without notice, so unknown keys are passed through.

    Cost is one extra request per accessible advertiser, once per sync.
    """

    name = "advertisers"
    path = "/advertiser/get/{advertiser_id}"
    primary_keys = ("advertiser_id",)
    records_jsonpath = "$"

    _money = 'Money as a currency-prefixed string, e.g. "GBP 10.00"'

    schema = th.PropertiesList(
        th.Property(
            "advertiser_id",
            th.StringType,
            required=True,
            description="Advertiser ID. Sourced from `id` in the API response.",
        ),
        th.Property(
            "role",
            th.StringType,
            description=(
                "The token holder's role on this advertiser, e.g. CLIENT_ADMIN. "
                "From /me, not from the advertiser record."
            ),
        ),
        th.Property("name", th.StringType, description="Advertiser name"),
        th.Property(
            "profile_id",
            th.StringType,
            description="Profile that owns this advertiser; joins to profiles",
        ),
        th.Property("website_url", th.StringType, description="Advertiser website"),
        th.Property(
            "categories",
            th.ArrayType(th.StringType),
            description='Business categories, e.g. "Energy & Utilities"',
        ),
        th.Property(
            "address",
            description="Registered address; fields are often blank",
            wrapped=th.ObjectType(
                th.Property("street_address", th.StringType),
                th.Property("street_address_2", th.StringType),
                th.Property("city", th.StringType),
                th.Property("state", th.StringType),
                th.Property("postal_code", th.StringType),
                th.Property("country", th.StringType),
            ),
        ),
        th.Property(
            "country",
            th.StringType,
            description='Country enum, e.g. UNITED_KINGDOM (address.country is "GB")',
        ),
        th.Property(
            "currency",
            th.StringType,
            description=(
                "Account currency, e.g. GBP. This is what the bare decimal "
                "money values on the report streams are denominated in."
            ),
        ),
        th.Property(
            "timezone",
            th.StringType,
            description=(
                "Account timezone, e.g. Europe/London. Explains why daily "
                "reporting windows land on 23:00:00Z in summer."
            ),
        ),
        th.Property("billing_limit", th.StringType, description=_money),
        th.Property("account_balance", th.StringType, description=_money),
        th.Property("payment_profile_id", th.StringType, description="Payment profile"),
        th.Property(
            "bill_to_payment_profile_id",
            th.StringType,
            description="Payment profile billed for this advertiser",
        ),
        additional_properties=True,
    ).to_dict()

    @cached_property
    def _accessible(self) -> dict[str, str]:
        """Return ``{advertiser_id: role}`` for every advertiser ``/me`` reports.

        Narrowed by the ``advertiser_ids`` setting when it is set.
        """
        response = self._request(
            self.build_prepared_request(
                method="GET",
                url=f"{self.url_base}/me",
                headers=self.http_headers,
            ),
            None,
        )
        user = response.json().get("user") or {}
        # The reference docs call this advertiser_id; the live API returns id.
        roles = {
            entry.get("id") or entry.get("advertiser_id"): entry.get("role")
            for entry in user.get("advertisers_with_access") or []
        }
        if selected := self.config.get("advertiser_ids"):
            roles = {k: v for k, v in roles.items() if k in selected}
            if missing := set(selected) - set(roles):
                self.logger.warning(
                    "advertiser_ids %s are not accessible to this token.",
                    sorted(missing),
                )
        return roles

    @property
    def partitions(self) -> list[dict] | None:
        """One partition per accessible advertiser, so each is fetched once."""
        return [{"advertiser_id": advertiser_id} for advertiser_id in self._accessible]

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

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Normalise the id and attach the role that only /me knows about."""
        row = super().post_process(row, context) or row
        advertiser_id = str(
            row.pop("id", None) or (context or {}).get("advertiser_id") or ""
        )
        row["advertiser_id"] = advertiser_id
        row["role"] = self._accessible.get(advertiser_id)
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

    A cheap-to-reason-about alternative to ``performance_report``: it has
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
    primary_keys = ("ad_id", "date")
    replication_key = "date"
    records_jsonpath = "$"
    parent_stream_type = AdStream
    # One shared bookmark rather than one per ad: with is_sorted left False the
    # SDK holds the starting value steady for the whole run and only finalises
    # it at the end, so ads synced later in the run still get the full window.
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
            "date",
            th.DateType,
            required=True,
            description="The day these metrics cover; replication key",
        ),
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
            "ctr",
            th.NumberType,
            description=(
                "Click-through rate as a percentage value, e.g. 0.557 means "
                "0.557%. Verified against clicks/impressions on live data."
            ),
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

    def get_records(self, context: Context | None) -> t.Iterable[dict]:
        """Request one day at a time, so the stream is a daily time series.

        The endpoint returns a single aggregate row for whatever window it is
        given, so day-level detail means one request per ad per day. Verified
        additive on live data: three consecutive single-day windows summed
        exactly to the equivalent three-day window.

        Args:
            context: The stream context, carrying the ad and advertiser ids.

        Yields:
            One record per day for this ad.
        """
        for day in self._days(context):
            for record in super().get_records({**(context or {}), "day": day}):
                # Stamped here rather than in post_process: the SDK calls
                # post_process with the context passed to _sync_records, which
                # has no knowledge of the day being requested.
                record["date"] = day.isoformat()
                yield record

    def _days(self, context: Context | None) -> list[date]:
        """Return the days to request, oldest first.

        Incremental syncs resume from the bookmark, less ``lookback_days`` -
        ad metrics are restated as conversions are attributed late, so recent
        days are deliberately re-fetched.
        """
        end = self.window_date("end_date")

        start = self.window_date("start_date")
        # get_starting_timestamp() would raise here: it insists the replication
        # key be a date-time, and `date` is a plain date. The raw bookmark value
        # is what we want anyway.
        if bookmark := self.get_starting_replication_key_value(context):
            resumed = as_local_date(str(bookmark)) - timedelta(
                days=self.config.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
            )
            start = max(start, resumed)

        if start > end:
            self.logger.warning(
                "%s: start_date (%s) is after end_date (%s); no days to sync.",
                self.name,
                start.isoformat(),
                end.isoformat(),
            )
            return []
        span = (end - start).days + 1
        return [start + timedelta(days=offset) for offset in range(span)]

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
            # start == end asks the API for exactly that one day.
            "start_time": context["day"].isoformat(),
            "end_time": context["day"].isoformat(),
        }

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Stamp the ad and advertiser ids onto the metrics row.

        ``date`` is stamped in :meth:`get_records`, which is the only place
        that knows which day was requested.
        """
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


# --------------------------------------------------------------------------
# The report builder: POST /api/v3/advertisers/{advertiserId}/reports
#
# Nextdoor has two report builders. The older one, POST /v2/api/reporting/create
# on the same base URL as every other stream here, accepts 8 dimensions and 21
# metrics. This stream targets the v3 endpoint instead, which accepts 29 and
# roughly 70 - adding creative-level breakdowns, demographics, household and
# member geo, per-conversion-event metrics and carousel metrics, none of which
# v2 can express. Auth is the same bearer token.
#
# The enums and request shape below are transcribed from the OpenAPI reference:
# https://developer.nextdoor.com/reference/post_api-v3-advertisers-advertiserid-reports
# The CSV column headers are undocumented on both versions. The names marked
# "verified" were confirmed against live v2 reports and are reused here because
# the two builders are the same product; everything else is the lowercased enum
# and is a guess. The generated schema allows additional properties, so a wrong
# guess passes the column through untyped rather than dropping it.
# --------------------------------------------------------------------------

#: The report builder lives on /api/v3, unlike every other stream in this tap.
V3_URL_BASE = "https://ads.nextdoor.com/api/v3"

#: Conversion events that appear both as a count metric and as a COST_PER_ one.
_CONVERSION_EVENTS = (
    "PURCHASE",
    "LEAD",
    "SIGN_UP",
    "ADD_TO_CART",
    "INITIATE_CHECKOUT",
    "SEARCH",
    "PAGE_VIEW",
    "VIEW_CONTENT",
    "ADD_TO_WISHLIST",
    "SUBSCRIBE",
)
_CUSTOM_CONVERSIONS = tuple(range(1, 11))

REPORT_DIMENSIONS = (
    # Time buckets are dimensions here, not a separate time_granularity field.
    "DAY",
    "WEEK",
    "MONTH",
    "CAMPAIGN",
    "CAMPAIGN_ID",
    "AD_GROUP",
    "AD_GROUP_ID",
    "AD",
    "AD_ID",
    "CREATIVE",
    "CREATIVE_ID",
    "PLACEMENT",
    "PLATFORM_TYPE",
    "HOUSEHOLD_SUBDIVISION",
    "HOUSEHOLD_DMA",
    "HOUSEHOLD_POSTAL_CODE",
    "HOUSEHOLD_CITY",
    "GENDER",
    "AGE",
    "HOUSEHOLD_INCOME",
    "HOMEOWNER",
    "DEVICE",
    "INTEREST",
    "USER_COUNTRY",
    "USER_STATE",
    "USER_DMA",
    "USER_POSTAL_CODE",
    "USER_CITY",
    "CONVERSION_EVENT_NAME",
)

REPORT_METRICS = (
    "CLICKS",
    "IMPRESSIONS",
    "UNIQUE_IMPRESSIONS",
    "AVG_FREQUENCY",
    "CTR",
    "SPEND",
    "CPM",
    "CPC",
    "CPA",
    "BILLABLE_SPEND",
    *(f"COST_PER_{event}" for event in _CONVERSION_EVENTS),
    *(f"COST_PER_CUSTOM_CONVERSION_{n}" for n in _CUSTOM_CONVERSIONS),
    *_CONVERSION_EVENTS,
    *(f"CUSTOM_CONVERSION_{n}" for n in _CUSTOM_CONVERSIONS),
    "CONVERSIONS",
    "VIEW_THROUGH_CONVERSIONS",
    "CLICK_THROUGH_CONVERSIONS",
    "LEAD_INFO",
    "LEAD_GEN_FORM_SUBMISSIONS",
    "LEAD_GEN_FORM_COMPLETION_RATE",
    "LEAD_GEN_FORM_CONFIRMATION_CTA_CLICKS",
    "VIDEO_FORMAT_PERCENT_25_VIEWS",
    "VIDEO_FORMAT_PERCENT_50_VIEWS",
    "VIDEO_FORMAT_PERCENT_75_VIEWS",
    "VIDEO_FORMAT_PERCENT_100_VIEWS",
    "VIDEO_FORMAT_SEC_2_VIEWS",
    "CAROUSEL_ENGAGEMENT",
    "CAROUSEL_CARD_IMPRESSIONS",
    "CAROUSEL_CARD_CLICKS",
    "CAROUSEL_CARD_CTR",
    "RESULT",
    "COST_PER_RESULT",
)

#: Requested when ``report.metrics`` is unset. Deliberately not the whole enum:
#: the twenty custom-conversion metrics and the carousel and lead-gen families
#: only mean anything on accounts configured for them, and asking for a metric
#: the account cannot serve is what the v2 builder raises
#: REPORT_BUILDER_INVALID_METRIC_FOR_REPORT_TYPE for. These eleven are the
#: delivery metrics confirmed to work on a live account.
REPORT_DEFAULT_METRICS = (
    "IMPRESSIONS",
    "CLICKS",
    "CTR",
    "SPEND",
    "BILLABLE_SPEND",
    "CPM",
    "CPC",
    "CPA",
    "CONVERSIONS",
    "RESULT",
    "COST_PER_RESULT",
)

REPORT_TYPES = (
    "DELIVERY_METRICS_REPORT",
    "LEAD_GEN_FORM_RESULTS_REPORT",
    "DCO_ENTITY_REPORT",
)
REPORT_FILTER_ATTRIBUTES = ("AD", "AD_GROUP", "CAMPAIGN", "PLACEMENT")
REPORT_FILTER_OPERATORS = ("CONTAINS",)

#: How often to log progress while waiting for a report. Generation can take
#: many minutes, and a silent wait is indistinguishable from a hung sync.
_POLL_LOG_INTERVAL_SECONDS = 30

#: Report statuses that will never become COMPLETED.
_FAILED_STATUSES = frozenset({"CANCELING", "CANCELED", "FAILED", "ARCHIVED"})

#: Dimensions that bucket rows in time. All three land in one ``date`` column.
_TIME_BUCKETS = frozenset({"DAY", "WEEK", "MONTH"})

#: The only time bucket the stream will replicate incrementally on. A DAY
#: report's ``date`` is a plain ISO date ("2026-06-01"), confirmed on live
#: output, so it parses back into a resumable bookmark. What WEEK and MONTH
#: put in that column is unverified - a bucket label like "2026-07" would not
#: parse - so those stay FULL_TABLE until one live run says otherwise.
_INCREMENTAL_TIME_BUCKET = "DAY"

#: Settings the v2 report builder took that v3 has no equivalent for, mapped to
#: what replaces them. A config carrying one would otherwise be silently
#: ignored and the report quietly built with defaults.
_RETIRED_REPORT_SETTINGS = {
    "dimension_granularity": "dimensions",
    "time_granularity": "dimensions (DAY, WEEK or MONTH is a dimension here)",
    "campaign_ids": 'filters, e.g. [{"attribute": "CAMPAIGN", "options": [...]}]',
    "adgroup_ids": 'filters, e.g. [{"attribute": "AD_GROUP", "options": [...]}]',
    "ad_ids": 'filters, e.g. [{"attribute": "AD", "options": [...]}]',
}

#: Dimension/metric pairs the API refuses to serve together, rejecting the
#: request with REPORT_BUILDER_CONFLICT_PARAMETER. Billable spend attaches
#: above the creative, so it cannot be split per creative; gross SPEND can.
#: Confirmed live. The API reports one conflict per request, so this list is
#: near-certainly incomplete - add pairs here as they surface.
_CONFLICTING_PAIRS = (("CREATIVE_ID", "BILLABLE_SPEND"),)

_TIME_BUCKET_DESCRIPTION = "The row's time bucket, at the requested granularity"

#: Column and description per dimension. The eight shared with the v2 builder
#: use header names verified against live v2 reports - note that PLATFORM_TYPE
#: arrives as `platform`, not as its enum name. The rest are unverified.
_DIMENSION_COLUMNS: dict[str, tuple[str, str]] = {
    # The id columns are verified against a live v3 report. The *name* columns
    # are where v3 diverges from v2: v2 returns "Ad Name" -> ad_name, v3
    # returns plain "Ad" -> ad. Only AD is directly confirmed; CAMPAIGN,
    # AD_GROUP and CREATIVE follow the same pattern by inference.
    "CAMPAIGN": ("campaign", "Campaign name"),
    "CAMPAIGN_ID": ("campaign_id", "Campaign ID; joins to the campaigns stream"),
    "AD_GROUP": ("ad_group", "Ad group name"),
    "AD_GROUP_ID": ("ad_group_id", "Ad group ID; joins to the ad_groups stream"),
    "AD": ("ad", "Ad name"),
    "AD_ID": ("ad_id", "Ad ID; joins to the ads and ad_stats streams"),
    "PLATFORM_TYPE": ("platform", 'Delivery platform, e.g. "On Platform"'),
    "PLACEMENT": ("placement", "Placement the metrics are attributed to"),
    # Unverified - v3 has no v2 counterpart to copy a header name from.
    "DAY": ("date", _TIME_BUCKET_DESCRIPTION),
    "WEEK": ("date", _TIME_BUCKET_DESCRIPTION),
    "MONTH": ("date", _TIME_BUCKET_DESCRIPTION),
    "CREATIVE": ("creative_name", "Creative name"),
    "CREATIVE_ID": ("creative_id", "Creative ID; joins to the creatives stream"),
    "HOUSEHOLD_SUBDIVISION": (
        "household_subdivision",
        "Subdivision (state/province) of the household",
    ),
    "HOUSEHOLD_DMA": ("household_dma", "Designated market area of the household"),
    "HOUSEHOLD_POSTAL_CODE": ("household_postal_code", "Postal code of the household"),
    "HOUSEHOLD_CITY": ("household_city", "City of the household"),
    "GENDER": ("gender", "Gender of the member the impression was served to"),
    "AGE": ("age", "Age bracket of the member"),
    "HOUSEHOLD_INCOME": ("household_income", "Household income bracket"),
    "HOMEOWNER": ("homeowner", "Whether the member is a homeowner"),
    "DEVICE": ("device", "Device the impression was served on"),
    "INTEREST": ("interest", "Interest the member was targeted on"),
    "USER_COUNTRY": ("user_country", "Country of the member"),
    "USER_STATE": ("user_state", "State/region of the member"),
    "USER_DMA": ("user_dma", "Designated market area of the member"),
    "USER_POSTAL_CODE": ("user_postal_code", "Postal code of the member"),
    "USER_CITY": ("user_city", "City of the member"),
    "CONVERSION_EVENT_NAME": (
        "conversion_event_name",
        "Name of the conversion event the row's conversions belong to",
    ),
}

#: Dimensions naming the same thing, most specific first. Only the first
#: requested member of a family joins the primary key, so asking for both
#: AD_ID and AD keys on the id rather than the ambiguous name - ad names are
#: not unique (4 of 37 distinct names in one test account were shared by two
#: ads each). Every dimension not listed here is its own family.
_GROUPED_DIMENSIONS = (
    ("DAY", "WEEK", "MONTH"),
    ("CAMPAIGN_ID", "CAMPAIGN"),
    ("AD_GROUP_ID", "AD_GROUP"),
    ("AD_ID", "AD"),
    ("CREATIVE_ID", "CREATIVE"),
)
_DIMENSION_FAMILIES = (
    *_GROUPED_DIMENSIONS,
    *(
        (dimension,)
        for dimension in REPORT_DIMENSIONS
        if not any(dimension in family for family in _GROUPED_DIMENSIONS)
    ),
)

#: CSV column per metric, for the metrics whose header is not simply the
#: lowercased enum. All verified against live v2 reports.
_METRIC_COLUMNS = {
    "SPEND": "gross_spend",
    "CONVERSIONS": "total_conversions",
    "VIDEO_FORMAT_SEC_2_VIEWS": "video_views_at_2_seconds",
    "VIDEO_FORMAT_PERCENT_25_VIEWS": "video_views_at_25",
    "VIDEO_FORMAT_PERCENT_50_VIEWS": "video_views_at_50",
    "VIDEO_FORMAT_PERCENT_75_VIEWS": "video_views_at_75",
    "VIDEO_FORMAT_PERCENT_100_VIEWS": "video_views_at_100",
}

#: Metrics returned as whole numbers.
_INTEGER_METRICS = frozenset(
    {
        "IMPRESSIONS",
        "CLICKS",
        "UNIQUE_IMPRESSIONS",
        "CONVERSIONS",
        "VIEW_THROUGH_CONVERSIONS",
        "CLICK_THROUGH_CONVERSIONS",
        "LEAD_GEN_FORM_SUBMISSIONS",
        "LEAD_GEN_FORM_CONFIRMATION_CTA_CLICKS",
        "VIDEO_FORMAT_SEC_2_VIEWS",
        "VIDEO_FORMAT_PERCENT_25_VIEWS",
        "VIDEO_FORMAT_PERCENT_50_VIEWS",
        "VIDEO_FORMAT_PERCENT_75_VIEWS",
        "VIDEO_FORMAT_PERCENT_100_VIEWS",
        "CAROUSEL_ENGAGEMENT",
        "CAROUSEL_CARD_IMPRESSIONS",
        "CAROUSEL_CARD_CLICKS",
        *_CONVERSION_EVENTS,
        *(f"CUSTOM_CONVERSION_{n}" for n in _CUSTOM_CONVERSIONS),
    }
)

#: Metrics arriving with a "%" suffix. The suffix is stripped but the value is
#: NOT rescaled: ad_stats returns CTR on the same percentage scale (0.557 for
#: 0.557%), so dividing by 100 here would make the two streams disagree.
_PERCENT_METRICS = frozenset(
    {"CTR", "LEAD_GEN_FORM_COMPLETION_RATE", "CAROUSEL_CARD_CTR"}
)

#: Placeholders the report puts in a numeric column when there is no value.
#: "N/A" is the one seen live - it appeared mid-report and took a ten-minute
#: sync down with it, so unrecognised non-numerics are nulled and warned about
#: rather than raised.
_NULL_CSV_VALUES = frozenset({"", "-", "--", "n/a", "na", "null", "none"})

#: Metrics that are not numeric at all. LEAD_INFO carries the submitted lead
#: details on a LEAD_GEN_FORM_RESULTS_REPORT.
_STRING_METRICS = frozenset({"LEAD_INFO"})

_METRIC_TYPES: dict[str, t.Any] = {
    **dict.fromkeys(_INTEGER_METRICS, th.IntegerType),
    **dict.fromkeys(_STRING_METRICS, th.StringType),
}


def _metric_descriptions() -> dict[str, str]:
    """Build the per-metric descriptions, generating the repetitive families."""
    descriptions = {
        "IMPRESSIONS": "Impressions served",
        "UNIQUE_IMPRESSIONS": "Distinct members an impression was served to",
        "AVG_FREQUENCY": "Average impressions per member reached",
        "CLICKS": "Clicks received",
        "CTR": (
            "Click-through rate as a percentage value, e.g. 1.05 means 1.05%. "
            'The CSV reports the string "1.05%"; only the suffix is stripped, '
            "so the scale matches ad_stats.ctr."
        ),
        "SPEND": "Gross spend, as a bare decimal in the account currency",
        "BILLABLE_SPEND": "Billable spend, as a bare decimal",
        "CPM": "Cost per thousand impressions, as a bare decimal",
        "CPC": "Cost per click, as a bare decimal",
        "CPA": "Cost per acquisition, as a bare decimal",
        "CONVERSIONS": "Total conversions attributed in the window",
        "VIEW_THROUGH_CONVERSIONS": (
            "Conversions attributed to an impression rather than a click"
        ),
        "CLICK_THROUGH_CONVERSIONS": "Conversions attributed to a click",
        "RESULT": "Results against the campaign objective",
        "COST_PER_RESULT": "Cost per result, as a bare decimal",
        "LEAD_INFO": (
            "Submitted lead details, on a LEAD_GEN_FORM_RESULTS_REPORT. Free "
            "text, so it is passed through as a string."
        ),
        "LEAD_GEN_FORM_SUBMISSIONS": "Lead gen form submissions",
        "LEAD_GEN_FORM_COMPLETION_RATE": (
            "Lead gen form completion rate as a percentage value; the CSV "
            'reports it as a string like "0.00%"'
        ),
        "LEAD_GEN_FORM_CONFIRMATION_CTA_CLICKS": (
            "Clicks on the lead gen form confirmation call to action"
        ),
        "VIDEO_FORMAT_SEC_2_VIEWS": "Video views reaching 2 seconds",
        "VIDEO_FORMAT_PERCENT_25_VIEWS": "Video views reaching 25%",
        "VIDEO_FORMAT_PERCENT_50_VIEWS": "Video views reaching 50%",
        "VIDEO_FORMAT_PERCENT_75_VIEWS": "Video views reaching 75%",
        "VIDEO_FORMAT_PERCENT_100_VIEWS": "Video views reaching 100%",
        "CAROUSEL_ENGAGEMENT": "Engagements with a carousel ad",
        "CAROUSEL_CARD_IMPRESSIONS": "Impressions of individual carousel cards",
        "CAROUSEL_CARD_CLICKS": "Clicks on individual carousel cards",
        "CAROUSEL_CARD_CTR": (
            "Carousel card click-through rate as a percentage value, e.g. "
            "1.05 means 1.05%"
        ),
    }
    for event in _CONVERSION_EVENTS:
        label = event.lower().replace("_", " ")
        descriptions[event] = f"{label.capitalize()} conversions attributed"
        descriptions[f"COST_PER_{event}"] = (
            f"Cost per {label} conversion, as a bare decimal"
        )
    for n in _CUSTOM_CONVERSIONS:
        descriptions[f"CUSTOM_CONVERSION_{n}"] = f"Custom conversion {n} events"
        descriptions[f"COST_PER_CUSTOM_CONVERSION_{n}"] = (
            f"Cost per custom conversion {n}, as a bare decimal"
        )
    return descriptions


_METRIC_DESCRIPTIONS = _metric_descriptions()


def _slug(value: str) -> str:
    """Turn a report name into a stream name, e.g. "Ad Performance Report".

    Args:
        value: The configured report name.

    Returns:
        A lower snake_case stream name, or "" if nothing usable remains.
    """
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _as_offset_datetime(day: date) -> str:
    """Render a plain date as the offset-bearing date-time the API demands.

    The report endpoint rejects a bare date - see
    :meth:`NextdoorStream.window_datetime`. Midnight UTC is used, matching how
    a date-only ``start_date``/``end_date`` setting is read.
    """
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat()


def _defaulted(value: int | str | None, default: int) -> int:
    """Return ``value`` as an int, or ``default`` when it is unset."""
    return default if value is None else int(value)


class PerformanceReportStream(NextdoorStream):
    """A custom performance report, defined entirely by the ``report`` config.

    Both the report definition and the stream's own name come from config, so
    one workspace can extract an ad-level report and another a creative-level
    one without any code change.

    Unlike every other stream here this one **writes**: it creates an ad hoc
    report on the advertiser's account, emails it to ``recipient_emails``, and
    returns a presigned ``download_url`` for the CSV, which this stream then
    downloads and emits row by row. Two consequences worth knowing:

    * Each sync creates a report object in the advertiser's account. They
      accumulate. (They are v3 objects, so they do *not* show up in the
      ``reports`` stream, which lists v2 report definitions.)
    * Each sync emails the recipients. Leave ``recipient_emails`` empty to skip
      the email while still generating the report.

    The report is built on Nextdoor's **v3** endpoint,
    ``POST /api/v3/advertisers/{advertiserId}/reports``, rather than the older
    ``POST /v2/api/reporting/create`` that the rest of this tap's base URL
    points at. That buys 29 dimensions and ~70 metrics instead of 8 and 21 -
    creative, demographics, household and member geo, per-conversion-event
    metrics. Four things follow from the version difference:

    * The advertiser is a **path** parameter, not a body field.
    * The time bucket is a **dimension** (``DAY``/``WEEK``/``MONTH``), not a
      separate setting, and all three land in the same ``date`` column.
    * Scoping is by ``filters`` - ``{attribute, operator: CONTAINS, options}``
      matching on *names*. There is no documented way to filter by id, so the
      v2 builder's ``campaign_ids``/``adgroup_ids``/``ad_ids`` have no
      equivalent; :data:`_RETIRED_REPORT_SETTINGS` rejects them explicitly
      rather than ignoring them.
    * Some dimension/metric pairs are refused together - see
      :data:`_CONFLICTING_PAIRS`. They are checked before the request so the
      failure lands at startup, not mid-sync.
    * The response carries a ``status``. The reference presents the call as
      synchronous, but the enum includes ``STARTED`` and ``IN_PROGRESS``, so
      the report is polled until it reports ``COMPLETED`` before the CSV is
      downloaded. A report that ends ``FAILED``/``CANCELED``/``ARCHIVED``, or
      is still running at ``max_poll_seconds``, raises rather than silently
      syncing zero rows.

    The CSV's column headers are normalised to snake_case (``"Ad ID"`` ->
    ``ad_id``). Because the header row is not documented, the schema allows
    additional properties so unexpected columns are passed through rather than
    dropped.

    **Replication.** INCREMENTAL on ``date`` when ``dimensions`` includes
    ``DAY``, otherwise FULL_TABLE - a report without a time bucket is a single
    aggregate row per entity for the whole window, with nothing to bookmark.
    This is the cheapest lever on the stream: generation time grows with the
    window, so resuming from the bookmark shortens the part that is actually
    slow. ``lookback_days`` of already-synced history is re-fetched, because
    report metrics are restated as conversions are attributed late.
    """

    #: Default stream name. Overridable via ``report.stream_name``, since the
    #: rows are only about ads when ``dimensions`` says so - a creative-level
    #: report may deserve a creative-level name.
    name = "performance_report"
    url_base = V3_URL_BASE
    path = "/advertisers/{advertiser_id}/reports"
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
        super().__init__(
            tap=tap,
            # None falls back to the class-level default.
            name=self._report.get("stream_name") or None,
            schema=self._build_schema(self._report),
            **kwargs,
        )
        self._primary_keys = (
            "advertiser_id",
            *self._key_columns(self._report["dimensions"]),
        )
        # Incremental only when the rows carry a day. The replication key has
        # to be decided here rather than declared on the class, because
        # whether there is a `date` column at all comes from config - see
        # _INCREMENTAL_TIME_BUCKET.
        if _INCREMENTAL_TIME_BUCKET in self._report["dimensions"]:
            self.replication_key = "date"
        elif set(self._report["dimensions"]) & _TIME_BUCKETS:
            self.logger.info(
                "%s: dimensions carry a WEEK/MONTH bucket rather than DAY, so "
                "the stream stays FULL_TABLE. The bucket value's format is "
                "unverified on this endpoint, and a bookmark cannot be "
                "resumed from a value that will not parse as a date.",
                self.name,
            )

    @property
    def report_config(self) -> dict[str, t.Any]:
        """Return the validated ``report`` config block, with defaults applied."""
        return self._report

    @staticmethod
    def _validated_report(configured: dict[str, t.Any]) -> dict[str, t.Any]:
        """Validate the configured report definition and apply defaults.

        Args:
            configured: The raw ``report`` config block.

        Returns:
            The block with defaults applied.

        Raises:
            ValueError: If a setting is one the v3 builder retired, or an enum
                value is not one it accepts.
        """
        if retired := sorted(set(configured) & set(_RETIRED_REPORT_SETTINGS)):
            replacements = ", ".join(
                f"report.{setting} -> use report.{_RETIRED_REPORT_SETTINGS[setting]}"
                for setting in retired
            )
            msg = (
                f"report setting(s) {retired} are not supported by the v3 "
                f"report endpoint this stream uses. {replacements}"
            )
            raise ValueError(msg)

        metrics = list(configured.get("metrics") or REPORT_DEFAULT_METRICS)
        dimensions = list(configured.get("dimensions") or ["DAY", "AD_ID", "AD"])
        report_type = configured.get("type") or "DELIVERY_METRICS_REPORT"
        name = configured.get("name") or "performance report"

        for values, allowed, label in (
            (metrics, REPORT_METRICS, "metrics"),
            (dimensions, REPORT_DIMENSIONS, "dimensions"),
            ([report_type], REPORT_TYPES, "type"),
        ):
            if invalid := [v for v in values if v not in allowed]:
                msg = (
                    f"Invalid report.{label} value(s) {invalid}. "
                    f"Supported values: {list(allowed)}"
                )
                raise ValueError(msg)

        for dimension, metric in _CONFLICTING_PAIRS:
            if dimension in dimensions and metric in metrics:
                # The SDK fills the config_jsonschema default in, so an
                # absent `metrics` key still arrives populated. Compare
                # against the default list rather than testing for presence,
                # or the message blames a setting the user never wrote.
                source = (
                    "the default metric list, REPORT_DEFAULT_METRICS"
                    if metrics == list(REPORT_DEFAULT_METRICS)
                    else "report.metrics"
                )
                msg = (
                    f"report.dimensions {dimension} and {metric} (from "
                    f"{source}) cannot be used together - the API rejects the "
                    "combination with REPORT_BUILDER_CONFLICT_PARAMETER. Drop "
                    "the dimension, or set report.metrics explicitly without "
                    f"{metric}."
                )
                raise ValueError(msg)

        window_days = configured.get("window_days")
        window_days = None if window_days in (None, 0) else int(window_days)
        if window_days is not None and window_days < 1:
            msg = f"report.window_days must be at least 1, got {window_days}."
            raise ValueError(msg)
        # Each slice is a separate report, so without a time dimension every
        # slice emits the same key with different values - the rows would
        # collide in the target rather than accumulate.
        if window_days is not None and not (set(dimensions) & _TIME_BUCKETS):
            msg = (
                "report.window_days splits the run into one report per slice, "
                "so report.dimensions must include a time bucket (DAY, WEEK "
                "or MONTH). Without one, every slice emits the same primary "
                "key and the rows collide."
            )
            raise ValueError(msg)

        filters = [
            PerformanceReportStream._validated_filter(entry)
            for entry in configured.get("filters") or []
        ]

        return {
            "metrics": metrics,
            "dimensions": dimensions,
            "type": report_type,
            "name": name,
            # The stream, and so the target table, is named after the report
            # being configured. An explicit stream_name wins; otherwise the
            # report's own name is slugified.
            "stream_name": configured.get("stream_name") or _slug(name),
            "recipient_emails": list(configured.get("recipient_emails") or []),
            "filters": filters,
            # Explicit None checks, not `or`: 0 is a meaningful interval.
            "window_days": window_days,
            "poll_interval_seconds": _defaulted(
                configured.get("poll_interval_seconds"), 5
            ),
            "max_poll_seconds": _defaulted(configured.get("max_poll_seconds"), 1800),
        }

    @staticmethod
    def _validated_filter(entry: dict[str, t.Any]) -> dict[str, t.Any]:
        """Validate one ``filters`` entry.

        Args:
            entry: A single configured filter.

        Returns:
            The filter, normalised to the three keys the API expects.

        Raises:
            ValueError: If the attribute or operator is not accepted.
        """
        attribute = entry.get("attribute")
        operator = entry.get("operator") or "CONTAINS"
        for value, allowed, label in (
            (attribute, REPORT_FILTER_ATTRIBUTES, "attribute"),
            (operator, REPORT_FILTER_OPERATORS, "operator"),
        ):
            if value not in allowed:
                msg = (
                    f"Invalid report.filters[].{label} value {value!r}. "
                    f"Supported values: {list(allowed)}"
                )
                raise ValueError(msg)
        return {
            "attribute": attribute,
            "operator": operator,
            "options": list(entry.get("options") or []),
        }

    @staticmethod
    def _key_columns(dimensions: list[str]) -> list[str]:
        """Return one key column per requested dimension family."""
        requested = set(dimensions)
        columns = []
        for family in _DIMENSION_FAMILIES:
            for member in family:
                if member in requested:
                    columns.append(_DIMENSION_COLUMNS[member][0])
                    break
        return columns

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
                description="ID of the generated report",
            ),
        ]

        # DAY, WEEK and MONTH all land in the same `date` column, so a config
        # naming more than one must not declare the property twice.
        seen: set[str] = set()
        for dimension in report["dimensions"]:
            column, description = _DIMENSION_COLUMNS[dimension]
            if column in seen:
                continue
            seen.add(column)
            properties.append(
                th.Property(
                    column,
                    th.DateType if column == "date" else th.StringType,
                    description=description,
                )
            )

        for metric in report["metrics"]:
            metric_type: t.Any = _METRIC_TYPES.get(metric, th.NumberType)
            properties.append(
                th.Property(
                    _METRIC_COLUMNS.get(metric, metric.lower()),
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
        """Build the report definition sent to the reports endpoint."""
        report = self.report_config
        window_start = (context or {}).get("window_start")
        payload: dict[str, t.Any] = {
            "name": (
                self._report_name(window_start, (context or {})["window_end"])
                if window_start
                else report["name"]
            ),
            "type": report["type"],
            # Only CSV is parsed here; XLSX is accepted by the API but would
            # need a binary reader.
            "output_format": "CSV",
            "date_time_range": self._date_time_range(context),
            "dimensions": report["dimensions"],
            "metrics": report["metrics"],
            "recipient_emails": report["recipient_emails"],
        }
        if report["filters"]:
            payload["filters"] = report["filters"]
        return payload

    def _date_time_range(self, context: Context | None) -> dict[str, str]:
        """Return the window for this request, as the endpoint wants it.

        The endpoint needs an offset-bearing date-time, unlike the /stats
        endpoints which take a plain LocalDate. Both bounds are exclusive at
        the top: the end is advanced a day, because ``end_date`` is documented
        as inclusive and reports run midnight to midnight.

        Args:
            context: The stream context. :meth:`get_records` puts the current
                slice on it; the config window is used if it has not.

        Returns:
            The ``date_time_range`` object for the request body.
        """
        window_start = (context or {}).get("window_start")
        if window_start is None:
            return {
                "start_date_time": self.window_datetime("start_date"),
                "end_date_time": self.window_datetime("end_date", plus_days=1),
            }
        return {
            "start_date_time": _as_offset_datetime(window_start),
            "end_date_time": _as_offset_datetime(
                (context or {})["window_end"] + timedelta(days=1),
            ),
        }

    def get_new_paginator(self) -> SinglePagePaginator:
        """Return a single-page paginator - one report per request."""
        return SinglePagePaginator()

    def get_records(self, context: Context | None) -> t.Iterable[dict]:
        """Request one report per window slice, oldest first.

        Generation time grows with the window and the number of dimensions, so
        a year at creative grain asked for in one report may never finish.
        ``report.window_days`` splits it into several smaller reports, which
        also means a failure costs one slice rather than the whole run.

        Args:
            context: The stream context, carrying the advertiser id.

        Yields:
            One record per CSV row, across every slice.
        """
        windows = self._windows(context)
        if len(windows) > 1:
            self.logger.info(
                "%s: %d window slices of up to %d days; that is %d reports "
                "created for advertiser %s this sync.",
                self.name,
                len(windows),
                self.report_config["window_days"],
                len(windows),
                (context or {}).get("advertiser_id"),
            )
        for start, end in windows:
            yield from super().get_records(
                {**(context or {}), "window_start": start, "window_end": end},
            )

    def _report_name(self, start: date, end: date) -> str:
        """Return the report's name in NAM, with its window stamped in.

        Report objects accumulate in the account, one per slice per sync, and
        the API puts neither a created-at timestamp nor a date range on them -
        a listed report carries only ``id``, ``name``, ``status``,
        ``dimensions``, ``metrics``, ``filters``, ``output_format`` and
        ``download_url``. The name is therefore the only thing that says what
        period a report covers, so the window goes in it.

        The tap's own stream name is unaffected: it comes from the configured
        ``name`` (or ``stream_name``), not from this.
        """
        return f"{self.report_config['name']} {start.isoformat()}..{end.isoformat()}"

    def _windows(self, context: Context | None) -> list[tuple[date, date]]:
        """Return the (start, end) date pairs to build a report for, inclusive.

        A single pair spanning the whole window when ``window_days`` is unset.

        On an incremental run the start is pulled forward to the bookmark,
        less ``lookback_days``. That is the main lever on this stream's cost:
        generation time grows with the window, so a daily run asks for a few
        days rather than regenerating the whole history.

        Args:
            context: The stream context, used to read the bookmark.

        Returns:
            The (start, end) pairs, oldest first.
        """
        start = self.window_date("start_date")
        end = self.window_date("end_date")

        # get_starting_timestamp() would raise: it insists the replication key
        # be a date-time, and `date` is a plain date. The raw bookmark value is
        # what we want anyway. Report metrics are restated as conversions are
        # attributed late, so recent buckets are deliberately re-fetched.
        if self.replication_key and (
            bookmark := self.get_starting_replication_key_value(context)
        ):
            resumed = as_local_date(str(bookmark)) - timedelta(
                days=self.config.get("lookback_days", DEFAULT_LOOKBACK_DAYS)
            )
            start = max(start, resumed)

        if start > end:
            # Either a misconfigured window, or an incremental run whose
            # bookmark has already passed end_date - both mean no report.
            self.logger.warning(
                "%s: resolved start (%s) is after end_date (%s); nothing to sync.",
                self.name,
                start.isoformat(),
                end.isoformat(),
            )
            return []

        size = self.report_config["window_days"]
        if not size:
            return [(start, end)]

        windows = []
        cursor = start
        while cursor <= end:
            slice_end = min(cursor + timedelta(days=size - 1), end)
            windows.append((cursor, slice_end))
            cursor = slice_end + timedelta(days=1)
        return windows

    def _fetch_report(self, advertiser_id: str, report_id: str) -> dict:
        """Re-read a report's record, to check whether it has finished."""
        response = self._request(
            self.build_prepared_request(
                method="GET",
                url=f"{self.url_base}/advertisers/{advertiser_id}/reports/{report_id}",
                headers=self.http_headers,
            ),
            None,
        )
        return response.json()

    def _await_completion(self, created: dict) -> dict:
        """Poll the created report until it reports ``COMPLETED``.

        Args:
            created: The body returned by the create call.

        Returns:
            The report record to take ``download_url`` from.

        Raises:
            RuntimeError: If the report reaches a terminal failure status, or
                is still running after ``max_poll_seconds``.
        """
        report = created
        status = report.get("status")
        advertiser_id = report.get("advertiser_id")
        report_id = report.get("id")

        # Undocumented shape, or nothing to poll with: trust what was handed
        # back rather than failing the sync on a technicality.
        if status is None or not (advertiser_id and report_id):
            return report

        budget = self.report_config["max_poll_seconds"]
        started = time.monotonic()
        last_logged = 0.0

        while status != "COMPLETED":
            if status in _FAILED_STATUSES:
                msg = f"Report {report_id} finished with status {status}."
                raise RuntimeError(msg)
            elapsed = time.monotonic() - started
            if elapsed >= budget:
                msg = (
                    f"Report {report_id} was still {status} after "
                    f"{budget}s. Raise report.max_poll_seconds, or narrow the "
                    "window."
                )
                raise RuntimeError(msg)
            if elapsed - last_logged >= _POLL_LOG_INTERVAL_SECONDS:
                last_logged = elapsed
                self.logger.info(
                    "Report %s is %s after %.0fs; waiting up to %ss.",
                    report_id,
                    status,
                    elapsed,
                    budget,
                )
            time.sleep(self.report_config["poll_interval_seconds"])
            report = self._fetch_report(advertiser_id, report_id)
            status = report.get("status")

        self.logger.info(
            "Report %s completed after %.0fs.", report_id, time.monotonic() - started
        )
        return report

    def parse_response(self, response: requests.Response) -> t.Iterable[dict]:
        """Wait for the report, download its CSV and yield one record per row.

        Args:
            response: The create-report response.

        Yields:
            One record per CSV data row.
        """
        yield from self._emit_csv(self._await_completion(response.json()))

    def _emit_csv(self, report: dict) -> t.Iterable[dict]:
        """Download a completed report's CSV and yield one record per row.

        Args:
            report: A report record carrying ``download_url``.

        Yields:
            One record per CSV data row.
        """
        download_url = report.get("download_url")
        if not download_url:
            self.logger.warning(
                "Report %s completed with no download_url; nothing to emit.",
                report.get("id"),
            )
            return

        # The URL is presigned, so it must be fetched without the tap's
        # Authorization header - S3 rejects requests carrying two auth methods.
        csv_response = requests.get(download_url, timeout=self.timeout)
        csv_response.raise_for_status()

        reader = csv.DictReader(io.StringIO(csv_response.text))
        emitted = 0
        for row in reader:
            record = {
                self._normalise_header(key): value
                for key, value in row.items()
                if key is not None
            }
            record["report_id"] = report.get("id")
            emitted += 1
            yield record

        if emitted:
            self.logger.info("Report %s: %d rows.", report.get("id"), emitted)
        else:
            # A report with no rows is not an error - the advertiser may simply
            # have had no delivery in the window - but it is indistinguishable
            # from a broken sync at the target, which writes no file at all.
            # The header row survives an empty report, and it is the only place
            # the CSV's real column names can be read, so log it: that is how
            # the inferred names in _DIMENSION_COLUMNS get confirmed.
            self.logger.warning(
                "Report %s downloaded with 0 data rows. Its CSV header was %s, "
                "normalising to %s. No rows means no output file from the "
                "target, which is expected when the advertiser had no "
                "delivery in the window.",
                report.get("id"),
                reader.fieldnames,
                [
                    self._normalise_header(f)
                    for f in reader.fieldnames or []
                    if f is not None
                ],
            )

    @staticmethod
    def _normalise_header(header: str) -> str:
        """Normalise a CSV header to snake_case, e.g. ``"Ad ID"`` -> ``ad_id``."""
        return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")

    def post_process(self, row: dict, context: Context | None = None) -> dict | None:
        """Stamp the advertiser id on the row and cast numeric metrics."""
        row = super().post_process(row, context) or row
        row["advertiser_id"] = (context or {})["advertiser_id"]

        for metric in self.report_config["metrics"]:
            if metric in _STRING_METRICS:
                continue
            column = _METRIC_COLUMNS.get(metric, metric.lower())
            row[column] = self._as_number(metric, column, row.get(column))
        return row

    def _as_number(
        self,
        metric: str,
        column: str,
        value: str | float | None,
    ) -> float | None:
        """Coerce one CSV cell to a number, or to None if it is not one.

        The CSV is untyped text and undocumented, so a cell can hold a
        placeholder like "N/A" where a number is expected. Nulling it keeps the
        rest of the report - and the rest of the sync - intact; raising would
        discard everything already fetched.

        Args:
            metric: The metric enum this column holds.
            column: The CSV column name, for the warning.
            value: The raw cell.

        Returns:
            An int, a float, or None.
        """
        if value is None:
            return None
        text = value.strip() if isinstance(value, str) else value
        if isinstance(text, str):
            if text.lower() in _NULL_CSV_VALUES:
                return None
            if metric in _PERCENT_METRICS:
                # "1.05%" -> 1.05, the same scale ad_stats reports CTR on
                text = text.removesuffix("%").strip()
        try:
            number = float(text)
        except (TypeError, ValueError):
            self.logger.warning(
                "Report column %s held %r, which is not a number; emitting "
                "null. Add it to _NULL_CSV_VALUES if it is a placeholder.",
                column,
                value,
            )
            return None
        return int(number) if metric in _INTEGER_METRICS else number
