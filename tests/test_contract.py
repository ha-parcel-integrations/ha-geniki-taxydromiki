"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from custom_components.geniki_taxydromiki import parcels
from custom_components.geniki_taxydromiki.calendar import (
    GenikiTaxydromikiDeliveriesCalendar,
)
from custom_components.geniki_taxydromiki.const import ParcelStatus
from custom_components.geniki_taxydromiki.parcels import (
    _LOCAL_TZ,
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)
from custom_components.geniki_taxydromiki.sensor import (
    GenikiTaxydromikiEnRouteToPickupPointSensor,
    GenikiTaxydromikiNextDeliverySensor,
)

from .payloads import checkpoint, result

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

SAMPLES = {
    "delivered": lambda: result(
        [
            checkpoint("C_NW", "2026-01-01T08:00:00"),
            checkpoint("C_P1", "2026-01-02T12:00:00"),
        ],
        status="DELIVERED",
        delivery_date="2026-01-02T12:00:00",
    ),
    "active": lambda: result([checkpoint("C_W3", "2026-01-01T08:00:00")]),
    "out_for_delivery": lambda: result([checkpoint("C_A3", "2026-01-01T08:00:00")]),
    "problem": lambda: result([checkpoint("C_EA_DP", "2026-01-01T08:00:00")]),
    "bare": lambda: result([]),
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_is_empty(parcel):
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_pickup_stays_true_after_collection(monkeypatch):
    monkeypatch.setitem(parcels._STATUS_MAP, "C_A1", ParcelStatus.AT_PICKUP_POINT)
    waiting = normalize_parcel(result([checkpoint("C_A1", "2026-01-01T08:00:00")]))
    assert waiting["status"] is ParcelStatus.AT_PICKUP_POINT
    assert waiting["pickup"] is True
    collected = normalize_parcel(
        result(
            [
                checkpoint("C_A1", "2026-01-01T08:00:00"),
                checkpoint("C_P1", "2026-01-02T12:00:00"),
            ],
            status="DELIVERED",
        )
    )
    assert collected["delivered"] is True
    assert collected["pickup"] is True
    assert collected["pickup_point"] is None


def test_is_pickup_prefers_the_route_flag():
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.IN_TRANSIT) is False
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.AT_PICKUP_POINT])


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=_LOCAL_TZ)
    assert start == "2099-04-29T13:00:00Z"
    stop = datetime.fromisoformat(end)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (23, 59, 59, 0)
    assert stop.date() == date(2099, 4, 29)


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=_LOCAL_TZ)
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    assert (first.hour, first.minute, first.second) == (0, 0, 0)
    assert (last.hour, last.minute, last.second) == (23, 59, 59)
    assert first.date() == last.date()


def test_planned_window_edge_cases():
    assert planned_window(None, None, zone=_LOCAL_TZ) == (None, None)
    assert planned_window(None, "2099-04-29", zone=_LOCAL_TZ)[0] is not None
    assert planned_window("bad", None, zone=_LOCAL_TZ) == (None, None)
    assert (
        planned_window(datetime(2099, 4, 29, 23, 59, 59), None, zone=_LOCAL_TZ)[1]
        is None
    )
    start, end = planned_window("2099-04-30", "2099-04-29", zone=_LOCAL_TZ)
    assert start.startswith("2099-04-29") and end.startswith("2099-04-30")
    assert planned_window(date(2099, 4, 29), date(2099, 4, 29), zone=_LOCAL_TZ)[1]
    assert planned_window(1, None, zone=_LOCAL_TZ) == (None, None)
    assert planned_window("2099-04-29T10:00:00", "2099-04-29T10:00:00", zone=_LOCAL_TZ)


@pytest.mark.parametrize(
    "status",
    [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING],
)
def test_planned_for_status_empties_done_parcels(status):
    assert planned_for_status(status, "a", "b") == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, "a", "b") == ("a", "b")


def _coordinator(data):
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _entry():
    entry = MagicMock()
    entry.entry_id = "e1"
    return entry


def _stub(barcode, **fields):
    return {
        "barcode": barcode,
        "status": ParcelStatus.OUT_FOR_DELIVERY,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "sender": None,
        "receiver": None,
        **fields,
    }


def test_next_delivery_skips_a_window_that_has_ended():
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    past_end = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    sensor = GenikiTaxydromikiNextDeliverySensor(
        _coordinator(
            [
                _stub("OLD", planned_from=past, planned_to=past_end),
                _stub("NEW", planned_from="2099-01-01T10:00:00Z"),
            ]
        ),
        _entry(),
    )
    assert sensor.extra_state_attributes["barcode"] == "NEW"


def test_en_route_excludes_returning_and_arrived():
    data = [
        _stub("A", pickup=True),
        _stub("B", pickup=True, status=ParcelStatus.RETURNING),
        _stub("C", pickup=True, status=ParcelStatus.AT_PICKUP_POINT),
    ]
    sensor = GenikiTaxydromikiEnRouteToPickupPointSensor(_coordinator(data), _entry())
    assert [p["barcode"] for p in sensor.extra_state_attributes["parcels"]] == ["A"]


def test_a_whole_day_window_is_an_all_day_event():
    calendar = GenikiTaxydromikiDeliveriesCalendar(
        _coordinator(
            [
                _stub(
                    "A",
                    planned_from="2099-04-29T00:00:00+03:00",
                    planned_to="2099-04-29T23:59:59+03:00",
                )
            ]
        ),
        _entry(),
    )
    (event,) = calendar._events()
    assert event.start == date(2099, 4, 29)
    assert event.end == date(2099, 4, 30)


def test_parse_iso_still_reads_offsets():
    assert parse_iso("2099-04-29T10:00:00+00:00") is not None
