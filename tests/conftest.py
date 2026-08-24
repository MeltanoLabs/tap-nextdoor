"""Shared fixtures for tap-nextdoor tests."""

from __future__ import annotations

import pytest

BASE = "https://ads.nextdoor.com/v2/api"

SAMPLE_CONFIG = {
    "access_token": "test-access-token",
    "advertiser_ids": ["adv1"],
    "start_date": "2025-01-01",
    "end_date": "2025-01-31",
    "page_size": 2,
}


def _envelope(key: str, records: list[dict], cursor: str = "") -> dict:
    """Wrap records in the NAM list-response envelope."""
    return {
        key: [{"cursor": f"c{i}", "data": r} for i, r in enumerate(records)],
        "page_info": {"end_cursor": cursor, "page_size": str(len(records))},
    }


def _campaign(i: int) -> dict:
    return {
        "id": f"camp{i}",
        "advertiser_id": "adv1",
        "name": f"Campaign {i}",
        "status": "ACTIVE",
        "user_status": "ACTIVE",
        "objective": "CONVERSION",
        "sub_objective": "WEBSITE_CONVERSIONS",
        "special_ad_category": False,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-02T00:00:00Z",
        # Java ZonedDateTime, as returned live.
        "start_time": "2025-01-01T00:01:34+01:00[Europe/London]",
    }


@pytest.fixture
def config() -> dict:
    """Return a sample tap config."""
    return dict(SAMPLE_CONFIG)


@pytest.fixture
def nam_api(requests_mock):
    """Mock every NAM endpoint the tap calls, with documented response shapes."""
    requests_mock.get(
        f"{BASE}/me",
        json={
            "user": {
                "id": "u1",
                "name": "Test User",
                "email": "test@example.com",
                "email_confirmed": True,
                # The live API returns `id` here, not the documented
                # `advertiser_id`.
                "advertisers_with_access": [
                    {"id": "adv1", "role": "CLIENT_ADMIN"},
                    {"id": "adv2", "role": "CLIENT_ADMIN"},
                ],
            },
            "profile": {
                "id": "p1",
                "name": "Test Profile",
                "associated_user_ids": ["u1"],
                "payment_profile_id": "pp1",
                "is_ad_agency": False,
            },
        },
    )
    # Two pages: a full page with a cursor, then a short final page.
    requests_mock.get(
        f"{BASE}/advertiser/campaign/list",
        [
            {"json": _envelope("campaigns", [_campaign(1), _campaign(2)], "CURSOR_A")},
            {"json": _envelope("campaigns", [_campaign(3)])},
        ],
    )
    requests_mock.get(
        f"{BASE}/adgroup/list",
        json=_envelope(
            "adgroups",
            [
                {
                    "id": "ag1",
                    "advertiser_id": "adv1",
                    "campaign_id": "camp1",
                    "name": "Ad group",
                    "status": "ACTIVE",
                    "user_status": "ACTIVE",
                    "placements": ["FEED", "FSF", "RHR"],
                    "audience_network_is_on": True,
                    "bid": {
                        "amount": "GBP 3.35",
                        "pricing_type": "CPM",
                        "bid_strategy": "MAX_BID",
                    },
                    "budget": {
                        "amount": "GBP 200",
                        "budget_type": "DAILY_CAP_MONEY",
                        "lifetime_delivery_cap_type": "DEFAULT",
                    },
                    "start_time": "2025-01-01T00:01:34+01:00[Europe/London]",
                    "frequency_caps": [],
                    "targeting": {
                        "included_location_targeting_ids": ["94103"],
                        "excluded_location_targeting_ids": [],
                        "audience_targeting_ids": [],
                        # Live shape: audiences are nested here, not in a
                        # top-level custom_audience_ids array.
                        # Nested targeting groups, as returned live.
                        "custom_audience_targeting": {
                            "include": [["ca1"]],
                            "exclude": [["ca1"]],
                        },
                        "interests_targeting": {"include": [], "exclude": []},
                        "geo_source_types": ["USER_PROFILE", "IP"],
                        "time_of_day": {"included": [], "excluded": []},
                    },
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-02T00:00:00Z",
                },
            ],
        ),
    )
    requests_mock.get(
        f"{BASE}/ad/list",
        json=_envelope(
            "ads",
            [
                {
                    "id": "ad1",
                    "advertiser_id": "adv1",
                    "adgroup_id": "ag1",
                    "creative_id": "cr1",
                    "name": "Ad",
                    "status": "ACTIVE",
                    "user_status": "ACTIVE",
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-02T00:00:00Z",
                },
            ],
        ),
    )
    requests_mock.get(
        f"{BASE}/advertiser/creative/list",
        json=_envelope(
            "creatives",
            [
                {
                    "id": "cr1",
                    "advertiser_id": "adv1",
                    "name": "Creative",
                    "status": "ACTIVE",
                    "placement": "FEED",
                    "creative_type": "IMAGE_NATIVE_V3",
                    "text_overlays": [],
                    "advertiser_name": "Acme",
                    "headline": "Headline",
                    "body_text": "Body",
                    "offer_text": "Offer",
                    "price": "9.99",
                    "cta": "LEARN_MORE",
                    "canvas_image_url": "https://example.com/c.png",
                    "logo_image_url": "https://example.com/l.png",
                    "clickthrough_url": "https://example.com",
                    "impression_tracking_urls": [],
                    "click_tracking_urls": [],
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-02T00:00:00Z",
                },
            ],
        ),
    )
    requests_mock.get(
        f"{BASE}/advertiser/reporting/list",
        json=_envelope(
            "reports",
            [
                {
                    "id": "r1",
                    "advertiser_id": "adv1",
                    "name": "Weekly report",
                    "download_url": "https://example.com/r.csv",
                },
            ],
        ),
    )
    requests_mock.get(
        f"{BASE}/ad/get/ad1/stats",
        json={
            "start_time": "2024-12-31T23:00:00Z",
            "end_time": "2025-01-31T23:00:00Z",
            "billable_spend": "GBP 12.50",
            "impressions": 1000,
            "clicks": 20,
            "total_conversions": 2,
            "ctr": 0.02,
            "cpc": "GBP 0.62",
            "cpm": "GBP 12.50",
            "purchase_conversions": 1,
            "lead_conversions": 1,
            "sign_up_conversions": 0,
            "add_to_cart_conversions": 0,
            "initiate_checkout_conversions": 0,
            "search_conversions": 0,
            "view_content_conversions": 0,
            "add_to_wishlist_conversions": 0,
            "subscribe_conversions": 0,
            "other_conversions": 0,
            "result": 2,
            "cost_per_result": "GBP 6.25",
        },
    )
    requests_mock.post(
        f"{BASE}/reporting/create",
        json={
            "id": "rep1",
            "advertiser_id": "adv1",
            "name": "tap-nextdoor ad performance",
            "download_url": "https://example.com/report.csv",
        },
    )
    requests_mock.get(
        "https://example.com/report.csv",
        # Header and value formats copied verbatim from a report created via
        # POST /reporting/create: Title Case headers, a Date column, names but
        # no IDs, "Gross Spend"/"Total Conversions", and CTR as a percentage.
        text=(
            "Campaign Name,Ad Group Name,Ad Name,Placement,Date,Impressions,"
            "Clicks,CTR,Gross Spend,Billable Spend,CPM,CPC,"
            "Total Conversions\n"
            "Campaign 1,Ad group,Ad,FEED,2026-07-01,"
            "72914,762,1.05%,373.36,371.93,5.10,0.49,2\n"
            "Campaign 1,Ad group,Ad,RHR,2026-07-02,"
            "293,1,0.34%,1.29,1.29,4.42,1.29,1\n"
        ),
    )
    requests_mock.get(
        f"{BASE}/custom_audience/get/ca1",
        json={
            "id": "ca1",
            "advertiser_id": "adv1",
            "name": "Audience",
            "description": "Description",
            "audience_type": "CUSTOMER_LIST",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-02T00:00:00Z",
        },
    )
    return requests_mock
