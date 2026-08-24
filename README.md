# tap-nextdoor

A [Singer](https://singer.io) tap for the Nextdoor Ads Manager (NAM) API, built with the [Meltano Singer SDK](https://sdk.meltano.com).

Built against the [Nextdoor Ads API reference](https://developer.nextdoor.com/reference/advertising-introduction). Base URL: `https://ads.nextdoor.com/v2/api`.

## API notes

Three traits of the NAM API shape this tap:

1. **List endpoints are `GET` requests with a JSON body.** Parameters like `advertiser_id`, `campaign_id` and `pagination_parameters` go in the request body, not the query string.
1. **Responses are enveloped.** Records arrive as `{"campaigns": [{"cursor": ..., "data": {...}}], "page_info": {...}}`, so each stream reads `$.<entity>[*].data`.
1. **Pagination is cursor-based** via `page_info.end_cursor`, echoed back as `pagination_parameters.cursor`. The API returns the same cursor on the final page, so the paginator stops on a short page.

## Where the live API differs from the docs

The schemas here were built from the reference docs and then corrected against live responses. Where the two disagree, this tap follows the live API:

| | Docs say | API actually returns |
|---|---|---|
| Reports path | `/advertiser/reportinglist` | `/advertiser/reporting/list` (the documented path 404s) |
| `/me` advertisers | `advertisers_with_access[].advertiser_id` | `id` (normalised to `advertiser_id` by the tap) |
| Ad group audiences | top-level `custom_audience_ids` | `targeting.custom_audience_targeting.{include,exclude}` |
| Stats `start_time`/`end_time` | `LocalDate` (`2024-01-01`) | full timestamps (`2026-06-30T23:00:00Z`) |
| Stats money fields | numbers | currency-prefixed strings (`"GBP 12.50"`) |
| Stats metrics | 9 fields | plus a conversion breakdown (`purchase_conversions`, `lead_conversions`, `result`, `cost_per_result`, ...) |

Two further quirks the tap handles:

- **Timestamps are Java `ZonedDateTime`** on campaigns and ad groups: `2025-08-26T00:01:34+01:00[Europe/London]`. The bracketed zone id makes the value invalid against JSON Schema's `date-time` format, so the tap strips it; the UTC offset is kept, so the instant is unchanged.
- **Money is always a currency-prefixed string** (`"GBP 3.35"`) on bids, budgets and all stats spend fields. These are typed as strings rather than silently parsed, so no currency information is lost.

Fields returned live but undocumented (`sub_objective`, `special_ad_category`, `creative_type`, `text_overlays`, `audience_network_is_on`, `bid.bid_strategy`, `budget.lifetime_delivery_cap_type`, `targeting.geo_source_types`, `targeting.interests_targeting`) are declared explicitly. The `ad_performance_reports` schema additionally allows unknown properties, since its response is undocumented in the OpenAPI definition.

## Streams

| Stream | Endpoint | Parent | Replication |
|---|---|---|---|
| `users` | `GET /me` | - | FULL_TABLE |
| `profiles` | `GET /me` | - | FULL_TABLE |
| `advertisers` | `GET /me` (`user.advertisers_with_access`) | - | FULL_TABLE |
| `campaigns` | `GET /advertiser/campaign/list` | `advertisers` | `updated_at` |
| `ad_groups` | `GET /adgroup/list` | `campaigns` | `updated_at` |
| `ads` | `GET /ad/list` | `ad_groups` | `updated_at` |
| `creatives` | `GET /advertiser/creative/list` | `advertisers` | `updated_at` |
| `reports` | `GET /advertiser/reporting/list` | `advertisers` | FULL_TABLE |
| `ad_stats` | `GET /ad/get/{id}/stats` | `ads` | FULL_TABLE |
| `ad_performance_reports` | `POST /reporting/create` + CSV download | `advertisers` | FULL_TABLE |
| `custom_audiences` | `GET /custom_audience/get/{id}` | `ad_groups` | `updated_at` |

### Notes on specific streams

- **`advertisers`** - the Ads API has no advertiser *list* endpoint (only `advertiser/create` and `advertiser/get/{id}/stats`), so accessible advertisers are read from `/me`'s `user.advertisers_with_access` and optionally narrowed with the `advertiser_ids` setting.

- **`ad_performance_reports`** - a **custom report defined entirely in config** (see below). This is the stream to use for ad performance: it can return a daily time series, which the stats endpoint cannot.

- **`ad_stats`** - the simpler, side-effect-free alternative. `/ad/get/{id}/stats` returns a single aggregate row per ad for the requested window, not a daily time series, so one request is made per ad for `start_date` -> `end_date`. That makes it the slowest stream: cost is one HTTP request per ad. Its schema was built from a live response, since the OpenAPI definition declares an empty object. The same `{entity}/get/{id}/stats` shape exists for advertisers, campaigns, ad groups and creatives if entity-level metrics are wanted later.

  For a daily time series, this stream would need to loop the window day-by-day (one request per ad per day) or use `POST /reporting/create` with `time_granularity: DAY` and fetch the resulting CSV. Neither is implemented.

- **`reports`** - lists saved/scheduled report definitions and their `download_url`; it contains no metrics. Creating reports (`POST /reporting/create`, with `dimension_granularity`, `time_granularity` and `metrics` enums) is a write operation and out of scope for a tap.

- **`custom_audiences`** - there is no list endpoint, so audiences are fetched by the ids found on their ad groups (`targeting.custom_audience_targeting`). Each id is requested once, even when shared across ad groups. Not yet exercised against an ad group that has audiences attached - every ad group seen so far has empty include/exclude lists.

## The `ad_performance_reports` stream

Unlike every other stream, this one **writes**. `POST /reporting/create` generates an ad hoc report, emails it to `recipient_emails`, and returns a presigned `download_url` for a CSV, which the tap downloads and emits row by row.

Two consequences:

- **Each sync creates a report object in the advertiser's account**, and they accumulate - the `reports` stream lists every one created so far. On an account with existing scheduled reports this can already be several thousand.
- **Each sync emails everyone in `recipient_emails`.** Leave it empty to skip the email; the field is not marked required in the OpenAPI schema, though the docs say the report "will be sent to the recipient emails".

The report is defined by the `report` config block:

```yaml
config:
  start_date: '2026-07-01'   # report window start
  end_date: '2026-07-31'     # inclusive
  report:
    metrics: [IMPRESSIONS, CLICKS, CTR, SPEND, BILLABLE_SPEND, CPM, CPC, CONVERSIONS]
    dimension_granularity: [AD]      # CAMPAIGN, AD_GROUP, AD, PLACEMENT
    time_granularity: [DAY]          # DAY, WEEK, MONTH
    name: tap-nextdoor ad performance
    recipient_emails: []             # every sync emails these
    campaign_ids: []                 # optional filters
    adgroup_ids: []
    ad_ids: []
```

All three enum lists are validated against the documented values before any request is made, so a typo fails with the supported values listed rather than a bare 400.

The **schema and primary key are derived from the config**: one column per requested dimension (`AD` -> `ad_id`, `ad_name`), one per requested metric, plus `advertiser_id`, `report_id` and `date`. The key is `advertiser_id` + `date` + the id column of each requested dimension. Money metrics (`SPEND`, `BILLABLE_SPEND`, `CPM`, `CPC`) are typed as strings, since the API returns them currency-prefixed; `IMPRESSIONS`/`CLICKS`/`CONVERSIONS` are cast to integers and `CTR` to a float.

CSV headers are normalised to snake_case (`"Ad ID"` -> `ad_id`). The header row is not documented anywhere, so the schema allows additional properties and unexpected columns pass through rather than being dropped. **This is the one part not yet verified against a live response** - see below.

### Not implemented

- **Targeting** - only `POST /targeting/geo/postal_code/bulk_match` exists, which is a lookup that takes input postal codes rather than an enumerable collection.
- **Media** - only upload endpoints exist (`media/logo/upload`, `media/canvas/upload`, `media/video/upload`). Creative image and logo URLs are available on the `creatives` stream.
- **Scheduled reports** - `POST /reporting/scheduled/create` sets up recurring emailed reports (`DAILY`/`WEEKLY`/`MONTHLY`/`QUARTERLY`). That is account configuration, not extraction, so it is out of scope.

## Configuration

| Setting | Required | Description |
|---|---|---|
| `access_token` | Yes | Ads API access token, generated in Nextdoor Ads Manager at https://ads.nextdoor.com/v2/manage/api |
| `advertiser_ids` | No | Filter advertisers (and their campaigns/ad groups/ads) by ID. Defaults to every advertiser reported by `/me` |
| `start_date` | No | Start of the `ad_performance_reports` window, as a date (`2025-01-01`). Defaults to today |
| `end_date` | No | End of that window, inclusive. Defaults to today |
| `page_size` | No | Records per page for the list endpoints. Defaults to 100 |

A full list of supported settings and capabilities is available by running:

```bash
tap-nextdoor --about
```

### Configure using environment variables

This Singer tap will automatically import any environment variables within the working directory's
`.env` if the `--config=ENV` is provided, such that config values will be considered if a matching
environment variable is set either in the terminal context or in the `.env` file.

### Source Authentication and Authorization

Access is granted via the [Ads and Conversion API Request Form](https://forms.gle/bbCSGUEuvrfxj3U58). Once approved, generate an access token in Nextdoor Ads Manager at https://ads.nextdoor.com/v2/manage/api. The tap sends it as `Authorization: Bearer <token>`.

## Usage

You can easily run `tap-nextdoor` by itself or in a pipeline using [Meltano](https://meltano.com/).

### Executing the Tap Directly

```bash
tap-nextdoor --version
tap-nextdoor --help
tap-nextdoor --config CONFIG --discover > ./catalog.json
```

### Test with Meltano

```bash
# Install
meltano install

# Test invocation:
meltano invoke tap-nextdoor --version

# OR run a test `elt` pipeline with the JSONL loader:
meltano run tap-nextdoor target-jsonl
```

## Developer Resources

### Initialize your Development Environment

```bash
uv sync
```

### Create and Run Tests

The test suite runs entirely against a mocked NAM API (see `tests/conftest.py`) and needs no credentials:

```bash
uv run pytest
```

You can also test the `tap-nextdoor` CLI interface directly using `uv run`:

```bash
uv run tap-nextdoor --help
```
