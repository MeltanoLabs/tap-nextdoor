# tap-nextdoor

A [Singer](https://singer.io) tap for the Nextdoor Ads Manager (NAM) API, built with the [Meltano Singer SDK](https://sdk.meltano.com).

## About Nextdoor Ads

[Nextdoor](https://nextdoor.com) is a neighbourhood social network. **Nextdoor Ads Manager (NAM)** is its self-serve advertising platform, where businesses run geographically targeted campaigns aimed at specific neighbourhoods, postal codes or radii, and measure them with the Nextdoor conversion pixel.

The **Ads API** exposes the NAM objects an advertiser manages - advertisers, campaigns, ad groups, ads, creatives, custom audiences - plus performance reporting. This tap extracts that data. Some NAM actions remain UI-only (initial sign-up, payment methods, adding or archiving custom audiences, archiving media assets), so they cannot be extracted or automated.

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

| Stream | Description | Endpoint | Parent | Replication |
|---|---|---|---|---|
| `users` | The NAM user that owns the access token, and which advertisers they can reach | `GET /me` | - | FULL_TABLE |
| `profiles` | The advertising profile behind the token, including its billing profile and whether it is an agency | `GET /me` | - | FULL_TABLE |
| `advertisers` | Advertiser accounts the token can access, with the token holder's role on each | `GET /me` (`user.advertisers_with_access`) | - | FULL_TABLE |
| `campaigns` | Campaigns, with objective, status and flight dates | `GET /advertiser/campaign/list` | `advertisers` | `updated_at` |
| `ad_groups` | Ad groups, carrying the bid, budget, placements, frequency caps and all targeting | `GET /adgroup/list` | `campaigns` | `updated_at` |
| `ads` | Individual ads, linking an ad group to the creative it renders | `GET /ad/list` | `ad_groups` | `updated_at` |
| `creatives` | Creative assets - headline, body, CTA, image and logo URLs, click and impression trackers | `GET /advertiser/creative/list` | `advertisers` | `updated_at` |
| `reports` | Saved and scheduled report definitions with their CSV download URLs. Definitions only, no metrics | `GET /advertiser/reporting/list` | `advertisers` | FULL_TABLE |
| `ad_stats` | Aggregate performance per ad for the configured window - spend, impressions, clicks, CTR, CPC, CPM and a conversion breakdown | `GET /ad/get/{id}/stats` | `ads` | FULL_TABLE |
| `ad_performance_reports` | A custom performance report defined in config: chosen metrics, broken down by chosen dimensions and time buckets. **Creates a report in the account and emails it** | `POST /reporting/create` + CSV download | `advertisers` | FULL_TABLE |
| `custom_audiences` | Custom audiences referenced by ad groups, with their type and description | `GET /custom_audience/get/{id}` | `ad_groups` | `updated_at` |

### Stream fields

Every field in every stream carries a description in its JSON schema, so the field documentation travels with the catalog rather than drifting from this README:

```bash
uv run tap-nextdoor --config=ENV --discover \
  | jq '.streams[] | select(.tap_stream_id=="campaigns") | .schema.properties
        | to_entries | map({field: .key, type: .value.type, description: .value.description})'
```

`ad_performance_reports` is the exception in that its schema is generated from your `report` config, so its fields depend on the metrics and dimensions you request - but those are described too.

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

The CSV header row is not documented anywhere. It was instead determined empirically, by downloading 48 existing report CSVs from a live account (a read-only operation - the `reports` stream already exposes their download URLs) and collecting the distinct header shapes:

```
campaign_id,campaign_name,ad_group_id,ad_group_name,ad_id,ad_name,placement,start_time,end_time,clicks,impressions,conversions,spend,billable_spend
campaign_id,campaign_name,start_time,end_time,spend
campaign_id,campaign_name,ad_group_id,ad_group_name,ad_id,ad_name,placement,start_time,clicks,impressions,conversions,spend,billable_spend
campaign_id,campaign_name,start_time,spend
```

Four things follow, all of which the schema reflects:

- Columns are **already snake_case**, so header normalisation is a no-op safeguard rather than a transformation.
- The ad group columns are **`ad_group_id`/`ad_group_name`**, even though every JSON endpoint calls the same field `adgroup_id`.
- The time bucket is **`start_time`** (plus `end_time` on reports spanning a range) - there is no `date` column. `start_time` is therefore part of the primary key.
- Money is a **bare decimal** (`26.87`), unlike the `/stats` endpoint's currency-prefixed `"GBP 0"`, so report metrics are numeric.

`start_time` is kept as a string because its format varies between reports - `2025-09-30` in some, `2025-09-06 12:00 AM` in others - and neither is RFC 3339. The schema still allows additional properties, so a column combination not seen in those 48 samples passes through rather than being dropped.

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

### Setup

Getting from nothing to a running sync:

1. **Request API access.** Submit the [Ads and Conversion API Request Form](https://forms.gle/bbCSGUEuvrfxj3U58). Access is granted per advertiser account and approval is not instant, so start here.
1. **Generate an access token.** Once approved, sign in to Nextdoor Ads Manager and go to <https://ads.nextdoor.com/v2/manage/api>. Generate a token and copy it - it is shown once.
1. **Set the token.** Put it in `TAP_NEXTDOOR_ACCESS_TOKEN` (see `.env.example`) or a `config.json`. Never commit it.
1. **Confirm the token works and find your advertiser IDs.**
   ```bash
   uv run tap-nextdoor --config=ENV --discover > catalog.json   # validates auth
   ```
   The `users` stream (`GET /me`) reports every advertiser the token can reach. Sync it alone to list them:
   ```bash
   uv run tap-nextdoor --config=ENV --catalog catalog.json | grep '"stream":"advertisers"'
   ```
1. **Narrow the scope (recommended).** Set `advertiser_ids` to just the accounts you want. Left empty, the tap syncs every advertiser the token can see, which multiplies runtime.
1. **Set the reporting window.** `start_date` and `end_date` bound `ad_performance_reports` and `ad_stats`. Both default to today, i.e. no history.
1. **Run it.**
   ```bash
   meltano run tap-nextdoor target-jsonl
   ```

### Security prerequisites

**Required permissions.** The access token inherits the permissions of the NAM user who generated it. That user needs a role granting access to each advertiser you intend to extract; roles appear on the `advertisers` stream as `role` (e.g. `CLIENT_ADMIN`). If an advertiser is missing from `advertisers`, the token has no access to it and no amount of configuration will surface its data - the fix is a NAM permission change, not a tap setting.

**Least privilege.** The Ads API has no read-only scope: the token is a bearer credential with the same reach as the user in the UI, and the same token that reads campaigns can also create them. Generate it from a user with access to only the advertisers being extracted.

**Handling the token.**

- It is long-lived, with no refresh flow. Treat it as a standing secret, rotate it on a schedule, and revoke it at <https://ads.nextdoor.com/v2/manage/api> if exposed.
- It is sent as `Authorization: Bearer <token>` over HTTPS on every request.
- Store it in a secrets manager or `.env` (gitignored), never in `meltano.yml`.

**Sensitive output.** Two things worth knowing before loading this into a shared warehouse:

- The `reports` stream's `download_url` is a presigned S3 URL with embedded AWS credentials. Short-lived, but a credential in a data column - consider deselecting the stream or masking the field.
- `users` and `profiles` carry personal data (name, email). Deselect them if you do not need them.

**Write access.** `ad_performance_reports` is the only stream that writes: it creates a report in the advertiser's account and emails it to `recipient_emails`. See its section above.

## Data recovery and backfill

**How replication works here.** `campaigns`, `ad_groups`, `ads`, `creatives` and `custom_audiences` replicate incrementally on `updated_at`. The reporting streams (`ad_performance_reports`, `ad_stats`) and the `/me`-derived streams are full-table and re-extract their whole window every run.

**Backfilling reporting data.** Widen the window and re-run; no state changes are needed, since these streams are full-table:

```bash
TAP_NEXTDOOR_START_DATE=2025-01-01T00:00:00Z \
TAP_NEXTDOOR_END_DATE=2025-12-31T00:00:00Z \
  meltano run tap-nextdoor target-jsonl
```

Cost scales with the window and the number of ads, so backfill in chunks (a month at a time) rather than one multi-year run. `ad_stats` in particular issues **one request per ad**.

**Recovering the incremental streams.** These are keyed on `updated_at`, so a full re-extract means clearing the bookmark:

```bash
# inspect
meltano state get dev:tap-nextdoor-to-target-jsonl

# reset one stream
meltano state clear dev:tap-nextdoor-to-target-jsonl --stream campaigns

# or reset everything
meltano state clear dev:tap-nextdoor-to-target-jsonl
```

Without Meltano, drop the affected stream from the `--state` file (or pass no state) and re-run.

**A caveat.** The API exposes no deletion or archival feed. Objects archived in NAM keep a `status` of `ARCHIVED` and are still returned, but anything hard-deleted simply stops appearing, and an incremental run will not tell the target it is gone. Periodically re-run without state if you need the destination to converge.

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
