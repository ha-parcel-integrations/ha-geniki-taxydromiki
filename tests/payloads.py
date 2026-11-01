"""Synthetic Geniki Taxydromiki payloads shared by the test modules.

Every value is invented: codes, names, shops and dates. Only the shapes
(object-vs-array behaviour, nullability, formats, sentinel dates) follow the
real reply. Keep them in one module so a shape change has exactly one place
to fix.
"""

from __future__ import annotations

ACTIVE_CODE = "1000000001"
DELIVERED_CODE = "1000000002"
UNKNOWN_CODE = "1000000003"

SENTINEL_DATE = "1900-01-01T00:00:00"


def checkpoint(code: str, stamp: str, text: str = "Scan") -> dict:
    """One entry of the carrier's checkpoint timeline."""
    return {
        "Status": text,
        "StatusCode": code,
        "StatusDate": stamp,
        "Shop": "SHOP A",
        "Latitude": "0",
        "Longitude": "0",
    }


def result(
    checkpoints: list[dict],
    *,
    code: str = ACTIVE_CODE,
    status: str = "IN TRANSIT",
    delivery_date: str = SENTINEL_DATE,
) -> dict:
    """A found-parcel result as the API client returns it.

    The reply does not repeat the tracking code; the coordinator adds it as
    ``trackingNumber`` before normalising, so the samples carry it too.
    """
    return {
        "Result": 0,
        "Checkpoints": {"Checkpoint": checkpoints},
        "Status": status,
        "DeliveryDate": delivery_date,
        "Consignee": "",
        "ReturningServiceVoucher": "",
        "DeliveredAt": "",
        "trackingNumber": code,
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel."""
    return result(
        [
            checkpoint("C_NW", "2026-01-01T09:00:00", "Registered"),
            checkpoint("C_H1", "2026-01-01T18:00:00", "Arrived at hub"),
            checkpoint("C_A3", "2026-01-02T08:30:00", "Out for Delivery"),
        ],
        code=code,
    )


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel, with a rescheduled attempt before the delivery."""
    sample = result(
        [
            checkpoint("C_NW", "2026-01-01T09:00:00", "Registered"),
            checkpoint("C_A3", "2026-01-02T08:30:00", "Out for Delivery"),
            checkpoint("C_EA_KS", "2026-01-02T15:00:00", "Delivery Rescheduled"),
            checkpoint("C_A3", "2026-01-03T08:30:00", "Out for Delivery"),
            checkpoint("C_W2", "2026-01-03T14:30:00", "Delivered"),
        ],
        code=code,
        status="DELIVERED",
        delivery_date="2026-01-03T14:30:00",
    )
    sample["Consignee"] = "Test Recipient"
    sample["DeliveredAt"] = "RECIPIENT"
    return sample


def envelope(code: str, inner: dict) -> dict:
    """The full HTTP reply wrapping one result."""
    return {"success": True, code: {"TrackAndTraceResult": inner}}


def not_found_envelope(code: str = UNKNOWN_CODE) -> dict:
    """The reply for a code the carrier has never issued."""
    return envelope(
        code,
        {"Result": 9, "Checkpoints": {}, "DeliveryDate": "0001-01-01T00:00:00"},
    )
