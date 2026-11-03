"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific: :data:`_STATUS_MAP` and
:func:`normalize_parcel`. Everything else — the timestamp parsing, the sort
contract, the delivered filter, the one-shot warning for unmapped statuses —
is suite-wide machinery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry

from .api import warn_once
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-geniki-taxydromiki/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the checkpoint ``StatusCode``; the free-text ``Status`` is
# localised and never used for mapping. There is no separate "accepted" value
# in the canonical enum, so depot scans are ``in_transit``.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "C_NW": ParcelStatus.REGISTERED,
    "C_SC": ParcelStatus.REGISTERED,
    "C_W3": ParcelStatus.IN_TRANSIT,
    "C_KK": ParcelStatus.IN_TRANSIT,
    "C_K8": ParcelStatus.IN_TRANSIT,
    "C_H1": ParcelStatus.IN_TRANSIT,
    "C_H2": ParcelStatus.IN_TRANSIT,
    "C_A1": ParcelStatus.IN_TRANSIT,
    "C_L1": ParcelStatus.IN_TRANSIT,
    "C_A3": ParcelStatus.OUT_FOR_DELIVERY,
    "C_W2": ParcelStatus.DELIVERED,
    "C_P1": ParcelStatus.DELIVERED,
    "C_S2": ParcelStatus.DELIVERED,
    "C_EA_DP": ParcelStatus.PROBLEM,
    "C_EA_KS": ParcelStatus.PROBLEM,
}

# Cancellation code -> the scan it retracts. The carrier's own page hides the
# pair; here both rows stay in the history, but neither may decide the
# parcel's status. A scan nothing has retracted counts like any other.
_CANCELLED_SCAN = {"C_L2": "C_L1", "C_E2": "C_E1", "C_K9": "C_K8"}

# Filed with the cancellations but never seen on a parcel. If one does undo a
# delivery, staying on the delivered scan would stop polling for good, so an
# older delivered scan no longer counts; a newer one still does.
_DELIVERY_CANCELLING_CODES = frozenset({"C_P4", "C_S5", "C_S8"})

# Seen in live replies; an unseen value is where a return or pickup state would
# surface first.
_KNOWN_HEADER_VALUES = {
    "Status": frozenset({"IN TRANSIT", "DELIVERED"}),
    "DeliveredAt": frozenset({"RECIPIENT"}),
}

_RESULT_KEYS = frozenset(
    {
        "Result",
        "Checkpoints",
        "Status",
        "DeliveryDate",
        "Consignee",
        "ReturningServiceVoucher",
        "DeliveredAt",
        "trackingNumber",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {"Status", "StatusCode", "StatusDate", "Shop", "Latitude", "Longitude"}
)

# The carrier stamps local wall-clock time without an offset.
_LOCAL_TZ = ZoneInfo("Europe/Athens")
_DATE_SENTINELS = ("1900-", "0001-")

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Geniki Taxydromiki status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{round(length)} x {round(width)} x {round(height)} cm",
    }


def _local_iso(value: Any) -> str | None:
    """Return an offset-aware ISO string for a carrier wall-clock stamp."""
    if not isinstance(value, str) or not value or value.startswith(_DATE_SENTINELS):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_LOCAL_TZ)
    # Only the label scan carries fractions; keep every row in one format.
    return parsed.replace(microsecond=0).isoformat()


def _checkpoints(raw: dict) -> list[dict]:
    """Return the checkpoints oldest to newest, tolerating odd shapes.

    A checkpoint with an unparseable date is kept (it can still decide the
    status) but sorts before every dated one so it never overrides them.
    """
    container = raw.get("Checkpoints")
    items = container.get("Checkpoint") if isinstance(container, dict) else None
    if isinstance(items, dict):
        warn_once(
            "singleton",
            "Geniki Taxydromiki sent a single checkpoint object instead of a list.",
        )
        items = [items]
    elif items is None:
        items = []
    elif not isinstance(items, list):
        warn_once(
            "checkpoints-shape",
            "Geniki Taxydromiki sent checkpoints as %s.",
            type(items).__name__,
        )
        items = []

    dated: list[tuple[datetime, dict]] = []
    undated: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("StatusCode")
        if code is not None and not isinstance(code, str):
            # This API sends an empty value as {}, which cannot be looked up.
            warn_once(
                "status-code-shape",
                "Geniki Taxydromiki sent a checkpoint code as %s.",
                type(code).__name__,
            )
            item = {**item, "StatusCode": None}
        extra = sorted(set(item) - _CHECKPOINT_KEYS)
        if extra:
            warn_once(
                f"checkpoint-keys-{extra}",
                "Geniki Taxydromiki checkpoints carry unexpected keys: %s.",
                extra,
            )
        timestamp = _local_iso(item.get("StatusDate"))
        parsed = parse_iso(timestamp)
        if parsed is None:
            warn_once("date", "Geniki Taxydromiki sent an unparseable checkpoint date.")
            undated.append(item)
        else:
            dated.append((parsed, {**item, "_timestamp": timestamp}))
    dated.sort(key=lambda pair: pair[0])
    return undated + [item for _, item in dated]


def build_history(
    checkpoints: list[dict], *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from sorted checkpoints.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. Oldest to newest, capped to the most recent
    ``max_events``.
    """
    history = [
        {
            "timestamp": item["_timestamp"],
            "status": map_event_status(item.get("StatusCode")),
            "raw_status": item.get("Status") or item.get("StatusCode"),
        }
        for item in checkpoints
        if item.get("_timestamp")
    ]
    return history[-max_events:]


def _deciding_status(checkpoints: list[dict]) -> ParcelStatus:
    """Status of the newest mappable checkpoint that is not part of a cancelling pair."""
    retracted: list[str] = []
    delivery_cancelled = False
    for item in reversed(checkpoints):
        code = item.get("StatusCode")
        if code in _CANCELLED_SCAN:
            retracted.append(_CANCELLED_SCAN[code])
            continue
        if code in retracted:
            retracted.remove(code)
            continue
        if code in _DELIVERY_CANCELLING_CODES:
            delivery_cancelled = True
            continue
        mapped = _STATUS_MAP.get(code)
        if mapped is ParcelStatus.DELIVERED and delivery_cancelled:
            continue
        if mapped is not None:
            return mapped
    return ParcelStatus.UNKNOWN


def _delivering_timestamp(checkpoints: list[dict]) -> str | None:
    """Timestamp of the newest dated checkpoint that maps to delivered."""
    for item in reversed(checkpoints):
        delivering = _STATUS_MAP.get(item.get("StatusCode")) is ParcelStatus.DELIVERED
        if delivering and item.get("_timestamp"):
            return item["_timestamp"]
    return None


def _warn_on_header(raw: dict, *, delivered: bool) -> None:
    """Warn once when the reply's header points at a state the scans don't show."""
    for field, known in _KNOWN_HEADER_VALUES.items():
        value = raw.get(field)
        if value and (not isinstance(value, str) or value not in known):
            warn_once(
                f"header-{field}",
                "Geniki Taxydromiki reports an unrecognised value in %s.",
                field,
            )
    if "Status" in raw and (raw["Status"] == "DELIVERED") != delivered:
        warn_once(
            "header-disagrees",
            "Geniki Taxydromiki's overall status and latest scan code disagree "
            "on whether a parcel is delivered.",
        )


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``raw`` is the carrier's ``TrackAndTraceResult`` plus the requested code
    under ``trackingNumber`` (the reply itself does not repeat it). The reply
    has no ETA, sender, pickup point, weight or dimensions, so those stay
    ``None``. The newest mappable checkpoint decides the status; the
    header ``Status`` text is never used for mapping.
    """
    tracking_code = raw.get("trackingNumber")
    checkpoints = _checkpoints(raw)

    extra = sorted(set(raw) - _RESULT_KEYS)
    if extra:
        warn_once(
            f"result-keys-{extra}",
            "Geniki Taxydromiki result carries unexpected keys: %s.",
            extra,
        )
    if raw.get("ReturningServiceVoucher"):
        warn_once(
            "returning-voucher",
            "Geniki Taxydromiki reports a returning service voucher; the "
            "return flow is not mapped.",
        )
    for item in checkpoints:
        code = item.get("StatusCode")
        if code and code not in _STATUS_MAP:
            _warn_unmapped_status(code)

    status = _deciding_status(checkpoints)
    delivered = status is ParcelStatus.DELIVERED
    _warn_on_header(raw, delivered=delivered)

    delivered_at = None
    if delivered:
        delivered_at = _delivering_timestamp(checkpoints)
        if raw.get("Status") == "DELIVERED":
            delivered_at = _local_iso(raw.get("DeliveryDate")) or delivered_at

    latest = checkpoints[-1] if checkpoints else {}
    return {
        "carrier": "Geniki Taxydromiki",
        "barcode": tracking_code,
        "sender": None,
        "receiver": (raw.get("Consignee") or None) if delivered else None,
        "status": status,
        "raw_status": latest.get("Status")
        or latest.get("StatusCode")
        or raw.get("Status")
        or None,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(checkpoints) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
