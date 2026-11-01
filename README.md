# Geniki Taxydromiki Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-geniki-taxydromiki.svg)](https://github.com/ha-parcel-integrations/ha-geniki-taxydromiki/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-geniki-taxydromiki/total.svg)](https://github.com/ha-parcel-integrations/ha-geniki-taxydromiki/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 📦 Part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/), a family of package-tracking integrations for Home Assistant that all publish the same parcel format, statuses and events, so they plug into cross-carrier automations and the optional [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator). Browse the other carriers on the website.
>
> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Geniki Taxydromiki](https://taxydromiki.com) parcels in Greece, Cyprus and Bulgaria. No account is needed — you enter the tracking code yourself, just like on the Geniki Taxydromiki website.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Geniki Taxydromiki parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text and a tracking deep-link
- Summary sensors: incoming parcels, recently delivered parcels
- `geniki_taxydromiki.track_parcel` / `geniki_taxydromiki.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- A Geniki Taxydromiki parcel (Greece, Cyprus or Bulgaria) and its tracking
  code (from the shipping confirmation or the voucher) — no account needed

## Installation

### HACS (recommended)

Click the button below to open this repository in HACS, then install **Geniki Taxydromiki**:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-parcel-integrations&repository=ha-geniki-taxydromiki&category=integration)

Or add it by hand: in HACS, choose the three-dot menu → **Custom repositories**, add `https://github.com/ha-parcel-integrations/ha-geniki-taxydromiki` as an **Integration** and install **Geniki Taxydromiki**.

Restart Home Assistant if HACS asks you to.

### Manual

Copy `custom_components/geniki_taxydromiki` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=geniki_taxydromiki)

Add the integration via **Settings → Devices & Services → Add Integration → Geniki Taxydromiki**. There is nothing to fill in: the hub is created immediately (Geniki Taxydromiki tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`geniki_taxydromiki.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Geniki Taxydromiki
  hub out there polls at exactly the same second.

Geniki Taxydromiki does not report a delivery window, so a parcel that is out for delivery is checked every 15 minutes until it is delivered.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Geniki Taxydromiki → ⋮ → Delete**. Nothing is stored on Geniki Taxydromiki's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.geniki_taxydromiki_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.geniki_taxydromiki_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.geniki_taxydromiki_next_delivery` | Earliest expected delivery moment; stays empty because Geniki Taxydromiki reports no delivery window |
| `sensor.geniki_taxydromiki_en_route_to_pickup_point` | Incoming parcels on their way to a pickup point that have not arrived yet; stays at zero until Geniki Taxydromiki reports a pickup-point state |
| `sensor.geniki_taxydromiki_awaiting_pickup` | Incoming parcels that are ready to collect at a pickup point; stays at zero until Geniki Taxydromiki reports a pickup-point state |
| `sensor.geniki_taxydromiki_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `calendar.geniki_taxydromiki_deliveries` | Read-only calendar of expected deliveries; stays empty because no delivery window is reported |
| `sensor.geniki_taxydromiki_last_successful_update` | Diagnostic: when Geniki Taxydromiki was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced / received by Geniki Taxydromiki |
| `in_transit` | In the carrier's network, including depot and service point scans |
| `out_for_delivery` | With the courier today |
| `delivered` | Delivered |
| `problem` | A delivery attempt failed or was rescheduled; the status follows the next scan |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

No separate pickup-point state is known for Geniki Taxydromiki yet, so a parcel waiting at a service point shows as `in_transit`, or as `problem` after a rescheduled delivery attempt there. Status codes the integration does not know are logged once as a warning and reported as `unknown`, falling back to the latest scan it does know.

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Geniki Taxydromiki device):

| Event | When |
|---|---|
| `geniki_taxydromiki_parcel_registered` | A new parcel appears in the active list |
| `geniki_taxydromiki_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `geniki_taxydromiki_parcel_delivered` | A parcel is delivered |
| `geniki_taxydromiki_parcel_delivery_time_changed` | Never fires, because no delivery window is reported |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `geniki_taxydromiki.track_parcel` | `tracking_code` | Start tracking a parcel |
| `geniki_taxydromiki.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.geniki_taxydromiki: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Geniki Taxydromiki has no scans for it yet, or the code is wrong. It will pick up automatically once scanned.
- **A status logs "Unrecognised Geniki Taxydromiki status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-geniki-taxydromiki/issues/new?template=unrecognised_status.yml) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Geniki Taxydromiki, Home Assistant, or any other third party referenced in this project. Please don't contact Geniki Taxydromiki for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Geniki Taxydromiki's terms. Data is sent only to Geniki Taxydromiki's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses the same public tracking endpoint as the Geniki Taxydromiki consumer website.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
