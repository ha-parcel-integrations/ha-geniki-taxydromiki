"""Geniki Taxydromiki public tracking API client.

Contract the coordinator relies on:

* ``async_get_parcel`` returns the carrier's per-parcel result dict on success,
* returns ``None`` when the carrier says the tracking code is unknown (a
  normal, expected state — never an error),
* raises :class:`GenikiTaxydromikiApiError` for anything else, with
  ``status_code`` set on a non-2xx response and ``retry_after`` set when the
  carrier's own ``Retry-After`` header on a 429 could be parsed as seconds —
  the coordinator's backoff reads both,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .const import TRACKING_API_URL

_LOGGER = logging.getLogger(__name__)

# A found code can take just over 30 seconds to answer while a not-found one
# takes under a second.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
_RESULT_FOUND = 0
_RESULT_NOT_FOUND = 9
# One request takes up to this many comma-separated codes.
BATCH_SIZE = 10

# Issue link logged with every shape surprise so a user can report it.
ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-geniki-taxydromiki/issues/new"
    "?template=unrecognised_status.yml"
)
_warned: set[str] = set()


def warn_once(key: str, message: str, *args: object) -> None:
    """Log a WARNING once per HA session, with the issue link appended."""
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning("%s Open an issue: %s", message % args, ISSUE_URL)


class GenikiTaxydromikiApiError(Exception):
    """Raised when a Geniki Taxydromiki API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Geniki Taxydromiki API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class GenikiTaxydromikiApiClient:
    """Client for the public Geniki Taxydromiki tracking endpoint.

    No authentication: the endpoint is keyed on the tracking code alone. It
    answers HTTP 200 with a JSON envelope keyed by the code that was sent::

        {"success": true, "<code>": {"TrackAndTraceResult": {"Result": 0, ...}}}

    ``Result`` 0 is a found parcel and 9 an unknown code.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the result dict for a known parcel, or ``None`` when the
        endpoint reports the code as unknown or has no scans for it. Any other
        outcome or non-2xx
        status raises :class:`GenikiTaxydromikiApiError`; network errors
        propagate as ``aiohttp.ClientError``.
        """
        payload = await self._async_post(tracking_code)
        return self._result(payload, tracking_code)

    async def async_get_parcels(
        self, tracking_codes: list[str]
    ) -> dict[str, dict[str, Any] | None]:
        """Fetch up to :data:`BATCH_SIZE` parcels with one request.

        A code the reply leaves out, or answers with a shape we cannot read,
        is fetched on its own, so one odd entry never fails the others.
        """
        if len(tracking_codes) == 1:
            code = tracking_codes[0]
            return {code: await self.async_get_parcel(code)}
        payload = await self._async_post(",".join(tracking_codes))
        results: dict[str, dict[str, Any] | None] = {}
        for code in tracking_codes:
            try:
                results[code] = self._result(payload, code)
            except GenikiTaxydromikiApiError:
                results[code] = await self.async_get_parcel(code)
        return results

    async def _async_post(self, voucher_no: str) -> dict[str, Any]:
        """Return the reply envelope for one or more comma-separated codes."""
        # The endpoint wants the bare JSON string as the body, not a form field.
        body = json.dumps(
            {"lang": "en", "voucherNo": voucher_no}, separators=(",", ":")
        )
        try:
            async with self._session.post(
                TRACKING_API_URL,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    try:
                        retry_after = (
                            float(retry_after_header) if retry_after_header else None
                        )
                    except ValueError:
                        retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                    raise GenikiTaxydromikiApiError(
                        "HTTP 429", status_code=429, retry_after=retry_after
                    )
                if response.status != 200:
                    raise GenikiTaxydromikiApiError(
                        f"HTTP {response.status}", status_code=response.status
                    )
                try:
                    # content_type=None: a challenge page or text/plain reply must
                    # surface as a parse failure, not an aiohttp content-type error.
                    payload = await response.json(content_type=None)
                except ValueError as err:
                    raise GenikiTaxydromikiApiError("unparseable body") from err
        except TimeoutError as err:
            # Not an aiohttp.ClientError, so it would otherwise fail the whole
            # poll for every other tracked parcel.
            raise GenikiTaxydromikiApiError("request timed out") from err

        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise GenikiTaxydromikiApiError("unexpected envelope")
        return payload

    @staticmethod
    def _result(payload: dict[str, Any], tracking_code: str) -> dict[str, Any] | None:
        """Read one code's result out of a reply envelope."""
        entry = payload.get(tracking_code)
        if not isinstance(entry, dict):
            warn_once(
                "envelope",
                "Geniki Taxydromiki reply is not keyed by the requested code "
                "(%d top-level keys).",
                len(payload),
            )
            raise GenikiTaxydromikiApiError("reply missing the requested code")
        result = entry.get("TrackAndTraceResult")
        if not isinstance(result, dict):
            warn_once(
                "result-object",
                "Geniki Taxydromiki reply has no TrackAndTraceResult object "
                "(keys: %s).",
                sorted(entry),
            )
            raise GenikiTaxydromikiApiError("reply missing TrackAndTraceResult")

        code = result.get("Result")
        if code == _RESULT_NOT_FOUND:
            return None
        if code != _RESULT_FOUND:
            warn_once(
                f"result-{code}",
                "Geniki Taxydromiki returned an unexpected Result value: %s.",
                code if isinstance(code, int) else type(code).__name__,
            )
            raise GenikiTaxydromikiApiError("unexpected Result")
        checkpoints = result.get("Checkpoints")
        if not isinstance(checkpoints, dict) or not checkpoints.get("Checkpoint"):
            # A known voucher without scans says nothing about the parcel, so
            # it reads like a code not scanned yet: the last good reply stays,
            # and a new code still gets its placeholder.
            warn_once(
                "no-checkpoints",
                "Geniki Taxydromiki returned a found code without checkpoints.",
            )
            return None
        return result
