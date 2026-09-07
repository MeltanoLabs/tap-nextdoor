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

Fields returned live but undocumented (`sub_objective`, `special_ad_category`, `creative_type`, `text_overlays`, `audience_network_is_on`, `bid.bid_strategy`, `budget.lifetime_delivery_cap_type`, `targeting.geo_source_types`, `targeting.interests_targeting`) are declared explicitly. The `performance_report` schema additionally allows unknown properties, since its response is undocumented in the OpenAPI definition.

## Streams

| Stream | Description | Endpoint | Parent | Replication |
|---|---|---|---|---|
| `users` | The NAM user that owns the access token, and which advertisers they can reach | `GET /me` | - | FULL_TABLE |
| `profiles` | The advertising profile behind the token, including its billing profile and whether it is an agency | `GET /me` | - | FULL_TABLE |
| `advertisers` | Advertiser accounts the token can access - name, website, categories, address, currency, timezone, billing - plus the token holder's role | `GET /me` + `GET /advertiser/get/{id}` | - | FULL_TABLE |
| `campaigns` | Campaigns, with objective, status and flight dates | `GET /advertiser/campaign/list` | `advertisers` | `updated_at` |
| `ad_groups` | Ad groups, carrying the bid, budget, placements, frequency caps and all targeting | `GET /adgroup/list` | `campaigns` | `updated_at` |
| `ads` | Individual ads, linking an ad group to the creative it renders | `GET /ad/list` | `ad_groups` | `updated_at` |
| `creatives` | Creative assets - headline, body, CTA, image and logo URLs, click and impression trackers | `GET /advertiser/creative/list` | `advertisers` | `updated_at` |
| `reports` | Saved and scheduled report definitions with their CSV download URLs. Definitions only, no metrics | `GET /advertiser/reporting/list` | `advertisers` | FULL_TABLE |
| `ad_stats` | Daily performance per ad - spend, impressions, clicks, CTR, CPC, CPM and a full conversion breakdown | `GET /ad/get/{id}/stats` | `ads` | `date` |
| `performance_report` | A custom performance report defined in config: chosen metrics, broken down by chosen dimensions - including creative, demographics and household/member geo. Renameable via `report.stream_name`. **Creates a report in the account and emails it** | `POST /api/v3/advertisers/{id}/reports` + poll + CSV download | `advertisers` | `date` (with `DAY`) |
| `custom_audiences` | Custom audiences referenced by ad groups, with their type and description | `GET /custom_audience/get/{id}` | `ad_groups` | `updated_at` |

### Stream fields

Every field in every stream carries a description in its JSON schema, so the field documentation travels with the catalog rather than drifting from this README:

```bash
uv run tap-nextdoor --config=ENV --discover \
  | jq '.streams[] | select(.tap_stream_id=="campaigns") | .schema.properties
        | to_entries | map({field: .key, type: .value.type, description: .value.description})'
```

`performance_report` is the exception in that its schema is generated from your `report` config, so its fields depend on the metrics and dimensions you request - but those are described too.

### Notes on specific streams

- **`advertisers`** - combines two endpoints. The Ads API has no advertiser *list* endpoint, so `/me` is the only way to discover which advertisers a token can reach; it returns just an id and the token holder's role. The detail comes from **`GET /advertiser/get/{id}`, which is undocumented** - it appears in neither the reference nor `llms.txt`, and was found by trying the `get/{id}` shape that campaigns and custom audiences use. Cost is one extra request per accessible advertiser, once per sync. Unknown keys are passed through, since an undocumented endpoint may change without notice.

  Two fields are worth calling out. **`currency`** is what the bare-decimal money values on `performance_report` are denominated in - nothing else in the API tells you. **`timezone`** explains why daily reporting windows land on `23:00:00Z` in summer (`Europe/London`).

  It also exposes **billing data** - `account_balance`, `billing_limit` and payment profile ids. Deselect the stream, or mask those fields, if that should not reach the warehouse.

- **`performance_report`** - a **custom report defined entirely in config** (see below). This is the stream to use for ad performance: it can return a daily time series, which the stats endpoint cannot.

- **`ad_stats`** - the side-effect-free daily series. The endpoint returns one aggregate row for whatever window it is given, so the stream walks the window **one day at a time**: `start_time == end_time` on every request. Verified additive against the API - three consecutive single-day windows summed exactly to the equivalent three-day window - and verified live end to end.

  It replicates incrementally on `date`. Because ad metrics are restated as conversions are attributed late, an incremental run restarts `lookback_days` (default 7) before the bookmark rather than trusting recent days as final. A single shared bookmark is used rather than one per ad; the stream is left unsorted, so the SDK holds the starting value steady for the whole run and only finalises it at the end, meaning ads synced later still get the full window.

  **Cost scales with ads x days: one request per ad per day.** For 271 ads that is 271 requests for a daily incremental run, but ~8,400 for a month-long backfill and ~99,000 for a year. Widen `start_date` deliberately, and prefer `performance_report` (a handful of requests per advertiser, whatever the window) when a conversion breakdown and real ad IDs are not needed.

  Its schema was built from a live response, since the OpenAPI definition declares an empty object. The same `{entity}/get/{id}/stats` shape exists for advertisers, campaigns, ad groups and creatives if entity-level metrics are wanted later.

  For a daily time series, this stream would need to loop the window day-by-day (one request per ad per day) or use the report builder with a `DAY` dimension and fetch the resulting CSV - which is what `performance_report` does. Looping the window here is not implemented.

- **`reports`** - lists saved/scheduled report definitions and their `download_url`; it contains no metrics. It lists v2 report definitions only, so reports created by `performance_report` - a v3 object - do not appear here.

- **`custom_audiences`** - there is no list endpoint, so audiences are fetched by the ids found on their ad groups (`targeting.custom_audience_targeting`). Each id is requested once, even when shared across ad groups. Not yet exercised against an ad group that has audiences attached - every ad group seen so far has empty include/exclude lists.
## The `performance_report` stream

Unlike every other stream, this one **writes**. It creates an ad hoc report on the advertiser's account, emails it to `recipient_emails`, and returns a presigned `download_url` for a CSV, which the tap downloads and emits row by row.

Two consequences:

- **Each sync creates a report object in the advertiser's account**, and they accumulate. They are v3 objects, so they do *not* appear in the `reports` stream, which lists v2 report definitions.
- **Each sync emails everyone in `recipient_emails`.** Leave it empty to skip the email; the field is not marked required in the OpenAPI schema.

### Why the v3 endpoint

Nextdoor has two report builders. The older `POST /v2/api/reporting/create` sits on the same base URL as every other stream in this tap and accepts **8 dimensions and 21 metrics**. This stream uses `POST /api/v3/advertisers/{advertiserId}/reports` instead, which accepts **29 and roughly 70**. Auth is the same bearer token.

What v3 buys you, none of which v2 can express:

- **Creative**: `CREATIVE`, `CREATIVE_ID`.
- **Demographics**: `GENDER`, `AGE`, `HOUSEHOLD_INCOME`, `HOMEOWNER`, `DEVICE`, `INTEREST`.
- **Geo**, at household and member level: `HOUSEHOLD_{SUBDIVISION,DMA,POSTAL_CODE,CITY}`, `USER_{COUNTRY,STATE,DMA,POSTAL_CODE,CITY}`.
- **`CONVERSION_EVENT_NAME`**, and per-event metrics: `PURCHASE`, `LEAD`, `SIGN_UP`, `ADD_TO_CART`, `INITIATE_CHECKOUT`, `SEARCH`, `PAGE_VIEW`, `VIEW_CONTENT`, `ADD_TO_WISHLIST`, `SUBSCRIBE` and `CUSTOM_CONVERSION_1..10`, each with a `COST_PER_*` twin.
- **Reach**: `UNIQUE_IMPRESSIONS`, `AVG_FREQUENCY`. **Attribution**: `VIEW_THROUGH_CONVERSIONS`, `CLICK_THROUGH_CONVERSIONS`. **Carousel**: `CAROUSEL_ENGAGEMENT`, `CAROUSEL_CARD_{IMPRESSIONS,CLICKS,CTR}`.

Two things the v2 builder had that v3 does not:

- **`APP_INSTALLS` and `COST_PER_INSTALL`** are not in the v3 metric enum.
- **ID-based filtering.** v2 took `campaign_ids`/`adgroup_ids`/`ad_ids`; v3's only documented operator is `CONTAINS` over entity *names*. If you need exact ID scoping, filter downstream instead.

### Configuration

```yaml
config:
  start_date: '2026-07-01'   # report window start
  end_date: '2026-07-31'     # inclusive
  report:
    dimensions: [DAY, AD_ID, AD]   # the time bucket is a dimension here
    metrics: [IMPRESSIONS, CLICKS, CTR, SPEND, BILLABLE_SPEND, CPM, CPC, CPA, CONVERSIONS, RESULT, COST_PER_RESULT]
    type: DELIVERY_METRICS_REPORT  # or LEAD_GEN_FORM_RESULTS_REPORT, DCO_ENTITY_REPORT
    name: tap-nextdoor performance report   # report name in NAM
    stream_name: performance_report         # rename the stream if you like
    recipient_emails: []                    # every sync emails these
    filters:                                # optional, matches on NAME not id
      - attribute: CAMPAIGN                 # AD, AD_GROUP, CAMPAIGN, PLACEMENT
        operator: CONTAINS
        options: [Brand]
    window_days: 31                # split long windows into monthly reports
```

`metrics`, `dimensions`, `type` and each `filters` entry are validated before any request is made, so a typo fails with the supported values listed rather than a bare 400.

**`metrics` does not default to the whole enum.** It defaults to the eleven delivery metrics confirmed against a live account. The custom-conversion, carousel and lead-gen families only mean anything on accounts configured for them, and asking for a metric the account cannot serve is what the v2 builder raises `REPORT_BUILDER_INVALID_METRIC_FOR_REPORT_TYPE` for.

**The time bucket is a dimension.** `DAY`, `WEEK` and `MONTH` go in `dimensions`, not in a separate setting, and all three land in the same `date` column.

**Configs written for the v2 builder are rejected, not ignored.** `dimension_granularity`, `time_granularity`, `campaign_ids`, `adgroup_ids` and `ad_ids` have no v3 equivalent, so the stream fails at startup naming each offender and its replacement rather than silently building the report with defaults.

### Some dimensions and metrics conflict

The API refuses certain combinations outright, with `REPORT_BUILDER_CONFLICT_PARAMETER`:

| Dimension | Metric | Why |
|---|---|---|
| `CREATIVE_ID` | `BILLABLE_SPEND` | Billable spend attaches above the creative, so it cannot be split per creative. Gross `SPEND` is fine. |

These are checked before the request is made, so a conflicting config fails at startup rather than mid-sync. The API reports **one conflict per request**, so the table above is near-certainly incomplete - it holds what has actually been observed live. Add new pairs to `_CONFLICTING_PAIRS` in `tap_nextdoor/streams.py` as they surface.

### Do you need `CREATIVE_ID`, or will a join do?

Often a join will. The `ads` stream already carries a scalar `creative_id`, so an ad-grain report joins to it:

```sql
select r.*, a.creative_id, c.name as creative_name
from performance_report r
join ads a on a.id = r.ad_id
join creatives c on c.id = a.creative_id
```

That is exact, not an approximation - one ad renders one creative. Ask for `CREATIVE_ID` when you want creative-grain rows without the join, or a dimension the join cannot supply, such as demographics or geo.

### Syncing more than a month: `window_days`

Report generation is asynchronous and its cost grows with **both** the window length and the number of dimensions. A year at `DAY x AD_ID x AD x PLACEMENT x CREATIVE_ID` asked for as one report may take far longer than any sensible timeout, and a single failure loses the whole run.

`window_days` splits the window into slices and builds **one report per slice**:

```yaml
config:
  start_date: '2025-09-01'
  end_date: '2026-08-31'
  report:
    dimensions: [DAY, AD_ID, AD, PLACEMENT, CREATIVE_ID]
    window_days: 31        # -> 12 reports per advertiser, each ~a month
```

Slices are contiguous and non-overlapping - `2025-09-01..2025-10-01`, `2025-10-02..2025-11-01`, and so on - so rows accumulate into one table without gaps or double-counting. Unset, the whole window goes in a single report, which is what you want for a short window.

Two constraints:

- **A time bucket is required.** `dimensions` must contain `DAY`, `WEEK` or `MONTH`. Without one, every slice emits the same primary key with different values and the rows collide instead of accumulating. This is rejected at startup.
- **Each slice is a report object.** `window_days: 31` over a year creates **12 reports per advertiser per sync**, and they accumulate in the account. Multiply by the number of advertisers - narrow `advertiser_ids` if you do not need them all.

Slices are synced oldest first, and the count is logged at the start of each advertiser.

### Incremental replication

The stream is **INCREMENTAL on `date`** when `dimensions` includes `DAY`, and FULL_TABLE otherwise. A report with no time bucket is a single aggregate row per entity for the whole window, so there is nothing to bookmark.

This is the cheapest lever on the stream's cost. Generation time grows with the window length, so resuming from the bookmark shortens the part that is actually slow - a daily run asks for a few days instead of regenerating the whole history.

`lookback_days` (default 7) of already-synced history is re-requested on every incremental run, because report metrics are **restated as conversions are attributed late**. The re-emitted rows carry the same primary key, so a target that upserts on it replaces the earlier values rather than double-counting. `date` is part of the key for exactly this reason.

The bookmark only ever moves the start *forward*: lowering `start_date` is not undone by an old bookmark, and a bookmark past `end_date` creates no report at all and logs why.

**`WEEK` and `MONTH` stay FULL_TABLE.** A `DAY` report's `date` is a plain ISO date (`2026-06-01`, confirmed on live output), which parses back into a resumable bookmark. What `WEEK` and `MONTH` put in that column is unverified - a bucket label like `2026-07` would not parse - so those are left on FULL_TABLE and log a line saying so. Confirming the value on one live run is all it takes to widen this; see `_INCREMENTAL_TIME_BUCKET`.

### The report is polled

The reference presents creation as synchronous and returns a `download_url` in the 200. But the same response carries a `status` whose enum includes `STARTED` and `IN_PROGRESS`, so that URL cannot be trusted to be ready. The tap therefore polls `GET /api/v3/advertisers/{advertiserId}/reports/{reportId}` every 5 seconds until the status is `COMPLETED`, then downloads.

- A report already `COMPLETED` on creation is downloaded immediately, with no poll.
- A report that ends `FAILED`, `CANCELED`, `CANCELING` or `ARCHIVED` **raises**, rather than silently syncing zero rows.
- A report still running at the ceiling **raises**, pointing at `window_days`. The interval (5s) and the ceiling (1800s, 30 minutes) are the module constants `POLL_INTERVAL_SECONDS` and `MAX_POLL_SECONDS`, **not settings**: the endpoint gives no progress signal to tune an interval against, and raising a ceiling is the wrong answer to hitting it - a report still generating after 30 minutes wants a narrower window, not a longer wait. Generation is genuinely slow: a month-long window at ad x creative x placement x day grain was still `IN_PROGRESS` after 5 minutes on a live account.
- Progress is logged every 30s while waiting, so a long generation is distinguishable from a hung sync.
- A response carrying no `status` at all is trusted as-is, so an undocumented shape does not fail the sync on a technicality.

### Schema, primary key and column names

The **schema and primary key are derived from the config**: one column per requested dimension, one per requested metric, plus `advertiser_id` and `report_id`. The key is `advertiser_id` plus one column per requested dimension *family* - asking for both `AD_ID` and `AD` keys on the id, because ad names are **not unique** (4 of 37 distinct names in one test account were shared by two ads each).

The CSV header row is undocumented on both API versions. Headers are **Title Case with spaces** (`"Ad Id"`), so normalisation to snake_case is load-bearing, not a safeguard. The column names in `_DIMENSION_COLUMNS` and `_METRIC_COLUMNS` come from two places:

- **Confirmed against a live v3 report**: `DAY` -> `date`, `AD_ID` -> `ad_id`, `AD` -> `ad`, `CREATIVE_ID` -> `creative_id`, `PLACEMENT` -> `placement`; and the metrics `SPEND` -> `gross_spend`, `CONVERSIONS` -> `total_conversions`, plus `impressions`, `clicks`, `ctr`, `cpm`, `cpc`, `cpa`, `result`, `cost_per_result`.
- **Inferred from that**: v3 names its *name* columns after the bare entity, where v2 used `<entity>_name` - the live report returns `Ad`, not `Ad Name`. `CAMPAIGN` -> `campaign`, `AD_GROUP` -> `ad_group` and `CREATIVE` -> `creative` follow the same pattern, but only `AD` is directly confirmed.
- **Still unverified**: the demographic and geo dimensions, and the conversion, carousel and lead-gen metrics. These are the lowercased enum - `GENDER` -> `gender`, and so on.

> The `<entity>_name` -> `<entity>` difference is exactly the kind of thing the spec does not tell you. It was found by running the stream and diffing the emitted keys against the schema, which is the check worth repeating for any dimension in the third group above.

Value formats, all verified on v2 and assumed unchanged:

- **`CTR` is a percentage string** (`"1.05%"`). The tap strips the suffix but does **not** rescale: `ad_stats` reports CTR on the same percentage scale (`0.5573934` for an ad with 246 clicks on 44,134 impressions), so dividing by 100 would make the two streams disagree. Both express CTR as a percentage value - `1.05` means 1.05%.
- All other money and rate metrics are **bare decimals** (`373.36`), unlike the `/stats` endpoint's currency-prefixed `"GBP 0"`. What currency they are denominated in comes from `advertisers.currency` - nothing else in the API tells you.
- **A numeric column can hold `"N/A"`.** Seen live, partway through a report. Recognised placeholders (`N/A`, `-`, `null`, empty) become null; anything else non-numeric also becomes null but logs a warning naming the column, so one odd cell cannot discard a report that took ten minutes to generate. Add new placeholders to `_NULL_CSV_VALUES`.
- `LEAD_INFO` is free text and is passed through as a string.

### Caveats - this stream is built from the spec, not from live traffic

The v2 builder's enums were read out of the API's own validation error and confirmed by creating a report with all of them. **Nothing about v3 has been confirmed that way.** Specifically unverified: whether the dimension and metric enums are complete, whether `type` is required, whether every metric is available without an account feature flag, and - most consequentially - the CSV column names listed as unverified above.

The schema **allows additional properties**, so a wrong column guess passes the value through untyped rather than dropping it. The cost is a declared-but-always-null column alongside an undeclared real one; if the guess was a key column, that is a null in the primary key.

**On the first live run, check the emitted columns against the schema** and correct `_DIMENSION_COLUMNS`/`_METRIC_COLUMNS` in `tap_nextdoor/streams.py` for anything that does not line up. Each is a one-line fix.

### The reporting window is a date-time here, not a date

The report endpoint and the `/{entity}/get/{id}/stats` endpoints disagree about time formats, and the reference docs describe both as `LocalDate`:

| Endpoint | Accepts | Rejects |
|---|---|---|
| `/{entity}/get/{id}/stats` | `2026-07-01` | - |
| the report endpoint | `2026-07-01T00:00:00Z`, `+00:00`, `+01:00[Europe/London]` | `2026-07-01` (*parsed at index 10*), `2026-07-01T00:00:00` (*index 19*) |

An offset is mandatory for the report endpoint. The tap sends the right form to each, so `start_date`/`end_date` behave the same to you regardless of stream. v3 nests them under `date_time_range` and renames both bounds, but the format is unchanged.

`end_date` is documented as inclusive, and reports run midnight to midnight (a one-day report spans `00:00` to the next `00:00`), so the tap advances the upper bound by one day. That inference comes from sampled v2 reports, not from documentation.

### Not implemented

- **Targeting** - only `POST /targeting/geo/postal_code/bulk_match` exists, which is a lookup that takes input postal codes rather than an enumerable collection.
- **Media** - only upload endpoints exist (`media/logo/upload`, `media/canvas/upload`, `media/video/upload`). Creative image and logo URLs are available on the `creatives` stream.
- **Scheduled reports** - `POST /reporting/scheduled/create` sets up recurring emailed reports (`DAILY`/`WEEKLY`/`MONTHLY`/`QUARTERLY`). That is account configuration, not extraction, so it is out of scope.

## Configuration

| Setting | Required | Description |
|---|---|---|
| `access_token` | Yes | Ads API access token, generated in Nextdoor Ads Manager at https://ads.nextdoor.com/v2/manage/api |
| `advertiser_ids` | No | Filter advertisers (and their campaigns/ad groups/ads) by ID. Defaults to every advertiser reported by `/me` |
| `start_date` | No | Start of the `performance_report` window, as a date (`2025-01-01`). Defaults to today |
| `end_date` | No | End of that window, inclusive. Defaults to today |
| `report` | No | Definition of the report built by `performance_report`. See its section above |
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
1. **Set the reporting window.** `start_date` and `end_date` bound `performance_report` and `ad_stats`. Both default to today, i.e. no history.
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

**Write access.** `performance_report` is the only stream that writes: it creates a report in the advertiser's account and emails it to `recipient_emails`. See its section above.

## Data recovery and backfill

**How replication works here.** `campaigns`, `ad_groups`, `ads`, `creatives` and `custom_audiences` replicate incrementally on `updated_at`; `ad_stats` replicates incrementally on `date`. `performance_report` and the `/me`-derived streams are full-table and re-extract their whole window every run.

**Backfilling reporting data.** Widen the window and re-run; no state changes are needed, since these streams are full-table:

```bash
TAP_NEXTDOOR_START_DATE=2025-01-01T00:00:00Z \
TAP_NEXTDOOR_END_DATE=2025-12-31T00:00:00Z \
  meltano run tap-nextdoor target-jsonl
```

Cost scales with the window and the number of ads, so backfill in chunks (a month at a time) rather than one multi-year run. `ad_stats` is the one to watch: it issues **one request per ad per day**, so a year-long backfill across 271 ads is roughly 99,000 requests.

**Recovering the incremental streams.** `campaigns`, `ad_groups`, `ads`, `creatives` and `custom_audiences` are keyed on `updated_at`, and `ad_stats` on `date`, so a full re-extract means clearing the bookmark:

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
