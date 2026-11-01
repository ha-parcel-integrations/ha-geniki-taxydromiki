"""Tests for Geniki Taxydromiki diagnostics."""

from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.geniki_taxydromiki.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.geniki_taxydromiki.parcels import normalize_parcel

from .payloads import delivered_sample


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": "1000000001"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [normalize_parcel(delivered_sample())]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    parcel = result["incoming"][0]
    for key in ("barcode", "receiver", "raw", "url", "delivered_at", "raw_status"):
        assert parcel[key] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert parcel["status"] == "delivered"
    assert "Test Recipient" not in str(result)


def test_every_key_of_the_reply_is_redacted():
    """A new field in the reply must be added to TO_REDACT, spelled as stored."""
    raw = delivered_sample()
    keys = set(raw) | set(raw["Checkpoints"]) | set(raw["Checkpoints"]["Checkpoint"][0])
    keys |= {"Checkpoint"}
    assert keys <= TO_REDACT


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
