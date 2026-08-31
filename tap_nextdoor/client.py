"""REST client handling, including NextdoorStream base class.

Built against the Nextdoor Ads API reference:
https://developer.nextdoor.com/reference/advertising-introduction

Two API traits shape this module:

1. The list endpoints are ``GET`` requests that carry a **JSON request body**
   (``advertiser_id``, parent ids, ``pagination_parameters``) rather than query
   string parameters.
2. Responses are enveloped as ``{"<entity>s": [{"cursor": ..., "data": {...}}],
   "page_info": {"end_cursor": ..., "page_size": ...}}``, so records live under
   ``$.<entity>s[*].data`` and the next-page cursor under
   ``$.page_info.end_cursor``.
"""

from __future__ import annotations

import typing as t
from datetime import date, datetime, timedelta, timezone
from functools import cached_property

from singer_sdk.helpers.jsonpath import extract_jsonpath
from singer_sdk.pagination import BaseAPIPaginator
from singer_sdk.streams import RESTStream

from tap_nextdoor.auth import NextdoorAuthenticator

if t.TYPE_CHECKING:
    import requests
    from singer_sdk.helpers.types import Auth, Context

DEFAULT_PAGE_SIZE = 100

#: Days of already-synced history the ad_stats stream re-fetches, because ad
#: metrics are restated as conversions are attributed after the fact.
DEFAULT_LOOKBACK_DAYS = 7


def as_local_date(value: str) -> date:
    """Parse an ISO-8601 date or date-time into a ``LocalDate``.

    The NAM reporting endpoints take ``LocalDate`` values ("2026-01-01"), but
    the ``start_date``/``end_date`` settings accept a full ISO-8601 date-time
    ("2026-01-01T00:00:00Z") so they behave like every other Singer tap.

    ``datetime.fromisoformat`` only learned to parse a trailing "Z" in Python
    3.11, and this package supports 3.10, so the offset is normalised first.

    Args:
        value: An ISO-8601 date or date-time string.

    Returns:
        The corresponding date.

    Raises:
        ValueError: If the value is not valid ISO-8601.
    """
    normalised = value.strip()
    if normalised.endswith(("Z", "z")):
        normalised = f"{normalised[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalised).date()
    except ValueError as exc:  # pragma: no cover - message clarity only
        msg = (
            f"Could not parse {value!r} as an ISO-8601 date or date-time "
            f"(e.g. 2026-01-01 or 2026-01-01T00:00:00Z)."
        )
        raise ValueError(msg) from exc


class NextdoorPaginator(BaseAPIPaginator["str | None"]):
    """Cursor paginator for the NAM API's ``page_info.end_cursor`` envelope.

    The cursor is echoed back in the request body as
    ``pagination_parameters.cursor``. The API does not expose a "has next page"
    flag and returns the same ``end_cursor`` on the final page, so pagination
    stops on a short page - the SDK raises ``RuntimeError`` on two identical
    consecutive tokens, which would otherwise surface as a sync failure.
    """

    def __init__(self, records_jsonpath: str, page_size: int) -> None:
        """Create a new paginator.

        Args:
            records_jsonpath: JSONPath to the record array, used to count the page.
            page_size: Requested page size, used to detect the final short page.
        """
        super().__init__(None)
        self._records_jsonpath = records_jsonpath
        self._page_size = page_size

    def has_more(self, response: requests.Response) -> bool:
        """Return True if a full page was returned and a cursor is present."""
        data = response.json()
        record_count = sum(1 for _ in extract_jsonpath(self._records_jsonpath, data))
        cursor = data.get("page_info", {}).get("end_cursor")
        return bool(cursor) and record_count >= self._page_size

    def get_next(self, response: requests.Response) -> str | None:
        """Return the cursor to send with the next request."""
        return response.json().get("page_info", {}).get("end_cursor")


class NextdoorStream(RESTStream):
    """Base stream class for the Nextdoor Ads Manager API."""

    url_base = "https://ads.nextdoor.com/v2/api"

    # The list endpoints are GET requests carrying a JSON body (see module
    # docstring). GET is already the SDK default, so only the body encoding
    # needs setting.
    payload_as_json = True

    #: Fields returned as Java ``ZonedDateTime`` strings with a bracketed zone
    #: id, which is stripped in :meth:`post_process`.
    zoned_datetime_fields: tuple[str, ...] = ("start_time", "end_time")

    @property
    def page_size(self) -> int:
        """Return the configured page size for list requests."""
        return self.config.get("page_size", DEFAULT_PAGE_SIZE)

    @cached_property
    def authenticator(self) -> Auth:
        """Return a new authenticator object.

        Returns:
            An authenticator instance.
        """
        return NextdoorAuthenticator.create_for_tap(self.config["access_token"])

    @property
    def http_headers(self) -> dict:
        """Return the http headers needed."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def window_date(self, setting: str) -> date:
        """Return the ``start_date``/``end_date`` setting as a ``LocalDate``.

        Defaults to today when unset. ``get_starting_timestamp()`` is not
        usable for this: the reporting streams have no replication key, so it
        would always return None.

        Args:
            setting: The config setting name to read.

        Returns:
            The configured date, or today.
        """
        if value := self.config.get(setting):
            return as_local_date(value)
        return datetime.now(tz=timezone.utc).date()

    def window_datetime(self, setting: str, *, plus_days: int = 0) -> str:
        """Return a window setting as an offset-bearing ISO-8601 date-time.

        ``POST /reporting/create`` rejects a bare date: "2026-07-01" fails with
        *could not be parsed at index 10*, and "2026-07-01T00:00:00" fails at
        index 19, so an offset is mandatory. (The ``/{entity}/get/{id}/stats``
        endpoints are the opposite - they take a plain ``LocalDate`` - which is
        why :meth:`window_date` exists alongside this.)

        A date-only setting is read as midnight UTC. A configured date-time is
        preserved, defaulting to UTC when it carries no offset of its own.

        Args:
            setting: The config setting name to read.
            plus_days: Days to add, used to turn an inclusive end date into the
                API's exclusive upper bound.

        Returns:
            An ISO-8601 date-time string including a UTC offset.
        """
        value = self.config.get(setting)
        if value:
            normalised = value.strip()
            if normalised.endswith(("Z", "z")):
                normalised = f"{normalised[:-1]}+00:00"
            moment = datetime.fromisoformat(normalised)
        else:
            today = datetime.now(tz=timezone.utc).date()
            moment = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment + timedelta(days=plus_days)).isoformat()

    def get_new_paginator(self) -> BaseAPIPaginator:
        """Create a new pagination helper instance.

        Returns:
            A pagination helper instance.
        """
        return NextdoorPaginator(self.records_jsonpath, self.page_size)

    def get_url_params(
        self,
        context: Context | None,  # noqa: ARG002
        next_page_token: str | None,  # noqa: ARG002
    ) -> dict[str, t.Any]:
        """Return query parameters. The NAM API takes its arguments in the body.

        Args:
            context: The stream context.
            next_page_token: The next page cursor, if any.

        Returns:
            An empty dictionary.
        """
        return {}

    def prepare_request_payload(
        self,
        context: Context | None,
        next_page_token: str | None,
    ) -> dict[str, t.Any] | None:
        """Build the JSON request body, including pagination parameters.

        Args:
            context: The stream context, carrying parent ids.
            next_page_token: The cursor returned by the previous page, if any.

        Returns:
            The JSON body to send with the request.
        """
        payload: dict[str, t.Any] = dict(self.get_body_params(context))

        pagination: dict[str, t.Any] = {"page_size": self.page_size}
        if next_page_token:
            pagination["cursor"] = next_page_token
        payload["pagination_parameters"] = pagination

        return payload

    def get_body_params(
        self,
        context: Context | None,
    ) -> dict[str, t.Any]:
        """Return the non-pagination body parameters for this stream.

        Every list endpoint requires ``advertiser_id``; child streams add their
        parent id by overriding this method.

        Args:
            context: The stream context.

        Returns:
            A dictionary of request body parameters.
        """
        return {"advertiser_id": (context or {})["advertiser_id"]}

    def post_process(
        self,
        row: dict,
        context: Context | None = None,  # noqa: ARG002
    ) -> dict | None:
        """Normalise Java ``ZonedDateTime`` strings into valid RFC 3339.

        The API returns values like
        ``2025-08-26T00:01:34+01:00[Europe/London]``. The bracketed zone id
        makes the string invalid against JSON Schema's ``date-time`` format,
        so it is stripped - the UTC offset is preserved, so the instant is
        unchanged.

        Args:
            row: An individual record.
            context: The stream context.

        Returns:
            The record, with zoned timestamps normalised.
        """
        for field in self.zoned_datetime_fields:
            value = row.get(field)
            if isinstance(value, str) and value.endswith("]") and "[" in value:
                row[field] = value[: value.index("[")]
        return row

    def parse_response(self, response: requests.Response) -> t.Iterable[dict]:
        """Parse the response and return an iterator of result records.

        Args:
            response: The HTTP ``requests.Response`` object.

        Yields:
            Each record from the source.
        """
        yield from extract_jsonpath(self.records_jsonpath, input=response.json())
