"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — ``parcels.py`` is free of I/O, so the
carrier-specific mapping can be tested as plain functions.
"""

from datetime import UTC, datetime, timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.geniki_taxydromiki import api, parcels
from custom_components.geniki_taxydromiki.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.geniki_taxydromiki.parcels import (
    _STATUS_MAP,
    apply_delivered_filter,
    build_history,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import SENTINEL_DATE, active_sample, checkpoint, delivered_sample, result


@pytest.fixture(autouse=True)
def _reset_one_shot_warnings():
    api._warned.clear()
    parcels._unmapped_statuses_logged.clear()


def _status(*codes: str) -> ParcelStatus:
    """Status of a parcel whose checkpoints are ``codes``, oldest first."""
    stamps = [f"2026-01-01T{hour:02d}:00:00" for hour in range(8, 8 + len(codes))]
    return normalize_parcel(
        result(
            [checkpoint(code, stamp) for code, stamp in zip(codes, stamps, strict=True)]
        )
    )["status"]


# ---------------------------------------------------------------------------
# status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("C_NW", ParcelStatus.REGISTERED),
        ("C_SC", ParcelStatus.REGISTERED),
        ("C_W3", ParcelStatus.IN_TRANSIT),
        ("C_KK", ParcelStatus.IN_TRANSIT),
        ("C_K8", ParcelStatus.IN_TRANSIT),
        ("C_H1", ParcelStatus.IN_TRANSIT),
        ("C_H2", ParcelStatus.IN_TRANSIT),
        ("C_A1", ParcelStatus.IN_TRANSIT),
        ("C_L1", ParcelStatus.IN_TRANSIT),
        ("C_A3", ParcelStatus.OUT_FOR_DELIVERY),
        ("C_W2", ParcelStatus.DELIVERED),
        ("C_P1", ParcelStatus.DELIVERED),
        ("C_S2", ParcelStatus.DELIVERED),
        ("C_EA_DP", ParcelStatus.PROBLEM),
        ("C_EA_KS", ParcelStatus.PROBLEM),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("C_ZZ") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("C_ZZ") is None
    assert map_event_status("C_W2") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("C_ZZ") == ParcelStatus.UNKNOWN
    assert map_parcel_status("C_ZZ") == ParcelStatus.UNKNOWN
    assert caplog.text.count("C_ZZ") == 1
    assert "issues/new" in caplog.text


def test_rescheduled_attempt_before_delivery_is_delivered():
    """The band-ordering trap: the newest checkpoint decides, not the worst one."""
    assert _status("C_NW", "C_A3", "C_EA_KS", "C_P1") == ParcelStatus.DELIVERED
    assert normalize_parcel(delivered_sample())["status"] == ParcelStatus.DELIVERED


def test_failed_attempt_after_out_for_delivery_is_a_problem():
    assert _status("C_NW", "C_A3", "C_EA_DP") == ParcelStatus.PROBLEM


def test_out_for_delivery_after_a_problem_recovers():
    assert _status("C_NW", "C_EA_DP", "C_A3") == ParcelStatus.OUT_FOR_DELIVERY


@pytest.mark.parametrize(
    ("pair", "before"),
    [
        (("C_L1", "C_L2"), "C_H1"),
        (("C_E1", "C_E2"), "C_A3"),
        (("C_K8", "C_K9"), "C_H2"),
    ],
)
def test_cancelling_pairs_never_decide_the_status(pair, before):
    assert _status("C_NW", before, *pair) == map_parcel_status(before)


@pytest.mark.parametrize("scan", ["C_L1", "C_K8"])
def test_a_scan_nothing_retracted_decides_the_status(scan):
    assert _status("C_NW", scan) == ParcelStatus.IN_TRANSIT
    assert _status("C_NW", "C_A3", scan) == ParcelStatus.IN_TRANSIT


def test_a_cancellation_retracts_only_one_scan():
    assert _status("C_NW", "C_A3", "C_L1", "C_L1", "C_L2") == ParcelStatus.IN_TRANSIT


@pytest.mark.parametrize("pair", [("C_L1", "C_L2"), ("C_K8", "C_K9")])
def test_a_cancelled_scan_does_not_override_out_for_delivery(pair):
    assert _status("C_NW", "C_A3", *pair) == ParcelStatus.OUT_FOR_DELIVERY


def test_a_scan_repeated_after_its_cancellation_decides():
    assert _status("C_NW", "C_A3", "C_L1", "C_L2", "C_L1") == ParcelStatus.IN_TRANSIT


@pytest.mark.parametrize("code", ["C_P4", "C_S5", "C_S8"])
def test_a_delivery_cancellation_undoes_older_delivered_scans(code, caplog):
    parcel = normalize_parcel(
        result(
            [
                checkpoint("C_A3", "2026-01-01T08:00:00"),
                checkpoint("C_W2", "2026-01-01T09:00:00"),
                checkpoint("C_P1", "2026-01-01T10:00:00"),
                checkpoint(code, "2026-01-01T11:00:00"),
            ]
        )
    )
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert code in caplog.text


def test_a_delivery_after_its_cancellation_counts():
    assert _status("C_A3", "C_P1", "C_P4", "C_W2") == ParcelStatus.DELIVERED


def test_a_cancellation_without_its_scan_does_not_decide():
    assert _status("C_NW", "C_A3", "C_L2") == ParcelStatus.OUT_FOR_DELIVERY


def test_cancelling_pair_rows_stay_in_the_history():
    parcel = normalize_parcel(
        result(
            [
                checkpoint("C_NW", "2026-01-01T08:00:00"),
                checkpoint("C_L1", "2026-01-01T09:00:00"),
                checkpoint("C_L2", "2026-01-01T10:00:00"),
            ]
        ),
        include_history=True,
    )
    assert [entry["status"] for entry in parcel["history"]] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.IN_TRANSIT,
        None,
    ]


def test_unmapped_newest_checkpoint_falls_back_to_newest_mappable(caplog):
    assert _status("C_NW", "C_A3", "C_EA_XX") == ParcelStatus.OUT_FOR_DELIVERY
    assert caplog.text.count("C_EA_XX") == 1


def test_only_unmapped_checkpoints_is_unknown(caplog):
    assert _status("C_ZZ") == ParcelStatus.UNKNOWN
    assert "C_ZZ" in caplog.text


def test_header_status_text_does_not_decide():
    raw = result([checkpoint("C_A3", "2026-01-01T08:00:00")], status="DELIVERED")
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_a_parcel_at_a_service_point_reads_in_transit():
    """No pickup-point code is known, so the last depot scan stays in transit."""
    parcel = normalize_parcel(
        result(
            [
                checkpoint("C_NW", "2026-01-01T08:00:00"),
                checkpoint("C_A1", "2026-01-02T08:00:00"),
            ]
        )
    )
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert parcel["pickup"] is False
    assert ParcelStatus.AT_PICKUP_POINT not in _STATUS_MAP.values()


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == UTC
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# checkpoints and history
# ---------------------------------------------------------------------------


def test_checkpoints_are_sorted_even_when_delivered_out_of_order():
    raw = result(
        [
            checkpoint("C_A3", "2026-01-02T08:00:00"),
            checkpoint("C_NW", "2026-01-01T08:00:00"),
        ]
    )
    history = normalize_parcel(raw, include_history=True)["history"]
    assert [entry["status"] for entry in history] == [
        ParcelStatus.REGISTERED,
        ParcelStatus.OUT_FOR_DELIVERY,
    ]
    # the raw order is untouched
    assert raw["Checkpoints"]["Checkpoint"][0]["StatusCode"] == "C_A3"


def test_history_timestamps_are_athens_local_time():
    history = normalize_parcel(delivered_sample(), include_history=True)["history"]
    assert history[0]["timestamp"] == "2026-01-01T09:00:00+02:00"
    assert history[0]["raw_status"] == "Registered"


def test_history_is_capped_to_the_most_recent_events():
    checkpoints = [
        checkpoint("C_H1", f"2026-01-{day:02d}T10:00:00") for day in range(1, 26)
    ]
    history = normalize_parcel(result(checkpoints), include_history=True)["history"]
    assert len(history) == 20
    assert history[-1]["timestamp"].startswith("2026-01-25")


def test_history_falls_back_to_status_code_without_text():
    parcel = normalize_parcel(
        result([{"StatusCode": "C_H1", "StatusDate": "2026-01-01T10:00:00"}]),
        include_history=True,
    )
    assert parcel["history"][0]["raw_status"] == "C_H1"


def test_undated_checkpoint_can_decide_but_is_not_in_history(caplog):
    raw = result([checkpoint("C_A3", "garbage")])
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["history"] == []
    assert "unparseable checkpoint date" in caplog.text
    assert "garbage" not in caplog.text


def test_undated_checkpoint_never_overrides_a_dated_one():
    raw = result(
        [checkpoint("C_W2", "garbage"), checkpoint("C_A3", "2026-01-01T08:00:00")]
    )
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY


@pytest.mark.parametrize("sentinel", ["1900-01-01T00:00:00", "0001-01-01T00:00:00"])
def test_placeholder_scan_date_is_undated(sentinel):
    raw = result(
        [checkpoint("C_H2", "2026-01-01T08:00:00"), checkpoint("C_A3", sentinel)]
    )
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["status"] == ParcelStatus.IN_TRANSIT
    assert [entry["raw_status"] for entry in parcel["history"]] == [
        raw["Checkpoints"]["Checkpoint"][0]["Status"]
    ]


@pytest.mark.parametrize("code", [{}, [], 5])
def test_status_code_of_another_type_is_tolerated(code, caplog):
    raw = result([checkpoint("C_H2", "2026-01-01T08:00:00")])
    raw["Checkpoints"]["Checkpoint"].append(
        {**checkpoint("C_A3", "2026-01-01T09:00:00"), "StatusCode": code}
    )
    assert (
        normalize_parcel(raw, include_history=True)["status"] == ParcelStatus.IN_TRANSIT
    )
    assert "checkpoint code as" in caplog.text


@pytest.mark.parametrize(
    ("field", "value"),
    [("Status", "RETURNED"), ("DeliveredAt", "SOMEONE"), ("Status", {"a": 1})],
)
def test_unseen_header_value_warns_without_the_value(field, value, caplog):
    raw = result([checkpoint("C_H2", "2026-01-01T08:00:00")])
    raw[field] = value
    assert normalize_parcel(raw)["status"] == ParcelStatus.IN_TRANSIT
    assert f"unrecognised value in {field}" in caplog.text
    assert str(value) not in caplog.text
    assert "issues/new" in caplog.text


def test_header_disagreeing_with_the_scans_warns(caplog):
    raw = result([checkpoint("C_A3", "2026-01-01T08:00:00")])
    raw["Status"] = "DELIVERED"
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert "disagree" in caplog.text


def test_receiver_stays_empty_until_delivered():
    raw = result([checkpoint("C_A3", "2026-01-01T08:00:00")])
    raw["Consignee"] = "Test Recipient"
    assert normalize_parcel(raw)["receiver"] is None


def test_fractional_scan_time_loses_its_fraction():
    raw = result([checkpoint("C_NW", "2026-01-01T08:00:00.55")])
    history = normalize_parcel(raw, include_history=True)["history"]
    assert history[0]["timestamp"] == "2026-01-01T08:00:00+02:00"


def test_build_history_skips_checkpoints_without_a_timestamp():
    assert build_history([{"StatusCode": "C_H1"}]) == []


def test_singleton_checkpoint_object_is_tolerated(caplog):
    raw = result([])
    raw["Checkpoints"]["Checkpoint"] = checkpoint("C_A3", "2026-01-01T08:00:00")
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert "single checkpoint object" in caplog.text
    assert "issues/new" in caplog.text


@pytest.mark.parametrize("shape", ["text", 5, None])
def test_odd_checkpoint_container_is_empty_and_warns_once(shape, caplog):
    raw = result([])
    raw["Checkpoints"]["Checkpoint"] = shape
    assert normalize_parcel(raw)["status"] == ParcelStatus.UNKNOWN
    normalize_parcel(raw)
    assert caplog.text.count("sent checkpoints as") == (0 if shape is None else 1)


def test_empty_checkpoints_object_is_unknown():
    raw = result([])
    raw["Checkpoints"] = {}
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "IN TRANSIT"


def test_non_dict_checkpoint_entries_are_ignored():
    raw = result(["junk", checkpoint("C_NW", "2026-01-01T08:00:00")])
    assert normalize_parcel(raw)["status"] == ParcelStatus.REGISTERED


def test_unexpected_keys_warn_without_values(caplog):
    raw = result(
        [{**checkpoint("C_NW", "2026-01-01T08:00:00"), "Driver": "secret-name"}]
    )
    raw["Extra"] = "secret-value"
    normalize_parcel(raw)
    assert "Extra" in caplog.text
    assert "Driver" in caplog.text
    assert "secret" not in caplog.text


def test_returning_service_voucher_warns_without_value(caplog):
    raw = active_sample()
    raw["ReturningServiceVoucher"] = "RET-SECRET"
    normalize_parcel(raw)
    assert "returning service voucher" in caplog.text
    assert "RET-SECRET" not in caplog.text


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Declared capabilities come true; everything else stays a literal None."""
    delivered = normalize_parcel(delivered_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    assert "url" in CAPABILITIES and delivered["url"] is not None
    assert "history" in CAPABILITIES and with_history["history"]
    for absent, key in (
        ("weight", "weight"),
        ("dimensions", "dimensions"),
        ("pickup_point", "pickup_point"),
    ):
        assert absent not in CAPABILITIES
        assert delivered[key] is None
    assert "delivery_window" not in CAPABILITIES
    assert delivered["planned_from"] is None
    assert delivered["planned_to"] is None
    assert "pickup_point" in PENDING_CAPABILITIES


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Geniki Taxydromiki"
    assert parcel["barcode"] == "1000000002"
    assert parcel["sender"] is None
    assert parcel["receiver"] == "Test Recipient"
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivered"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-03T14:30:00+02:00"
    assert parcel["url"] == "https://taxydromiki.com/track/1000000002"
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_active_parcel():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["raw_status"] == "Out for Delivery"
    assert parcel["receiver"] is None
    assert parcel["pickup"] is False


@pytest.mark.parametrize("sentinel", [SENTINEL_DATE, "0001-01-01T00:00:00", None, 5])
def test_delivered_at_falls_back_to_the_delivering_scan(sentinel):
    raw = delivered_sample()
    raw["DeliveryDate"] = sentinel
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == parcel["history"][-1]["timestamp"]


def test_delivered_at_ignores_the_date_without_the_delivered_header_status():
    raw = delivered_sample()
    raw["Status"] = "IN TRANSIT"
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["delivered_at"] == parcel["history"][-1]["timestamp"]


def test_delivered_at_with_malformed_date_falls_back():
    raw = delivered_sample()
    raw["DeliveryDate"] = "soon"
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["delivered_at"] == parcel["history"][-1]["timestamp"]


def test_delivered_at_is_none_without_any_dated_delivering_scan():
    raw = result([checkpoint("C_W2", "garbage")])
    assert normalize_parcel(raw)["delivered_at"] is None


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-known code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": "1000000009"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["history"] is None


def test_normalize_keeps_the_complete_raw_payload():
    raw = delivered_sample()
    snapshot = repr(raw)
    parcel = normalize_parcel(raw, include_history=True)
    assert parcel["raw"] is raw
    assert repr(raw) == snapshot


def test_status_map_values_come_from_the_enum():
    assert all(isinstance(value, ParcelStatus) for value in _STATUS_MAP.values())


def test_warnings_use_the_logger_at_warning_level(caplog):
    with caplog.at_level(logging.DEBUG):
        normalize_parcel(result([checkpoint("C_ZZ", "2026-01-01T08:00:00")]))
    assert all(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels


def test_checkpoint_without_a_date_is_undated(caplog):
    raw = result([{"StatusCode": "C_A3"}])
    assert normalize_parcel(raw, include_history=True)["history"] == []


def test_tracking_url_needs_a_code():
    assert parcels.tracking_url(None) is None
    assert parcels.tracking_url("") is None
