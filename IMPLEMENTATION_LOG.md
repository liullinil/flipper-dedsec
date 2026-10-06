# Implementation log

This file records the work completed in the DedSec Uplink and RF Signal
Hunter workspace, the decisions behind it, and the checks that still require
physical hardware or product work.

## Completed

- Restored the Flipper settings model to four font choices: **Normal**,
  **Large**, **Small** and **Micro**. Normal and Large use the bundled
  Cyrillic 6x12 font (with different list density), while the two compact
  modes use the SDK secondary and keyboard fonts. The choice is persisted in
  `.uplink.settings`; themes remain removed from the user-facing settings.
- Fixed vertical orientation. `ViewDispatcher` already applies the selected
  `ViewOrientation` to the Canvas, so the extra rotation in the draw callback
  was removed. Main, keyboard and settings views receive the same persisted
  orientation when it changes and when the app starts.
- Added a real RF settings page for dwell, RSSI threshold, capture window,
  silence threshold, band profile, timezone, retention and reserve space.
  Values are clamped to safe ranges and saved atomically. The capture window
  now closes a burst even when a long silence edge is not observed.
- Implemented passive NFC field-on/field-off event persistence. The FAP uses
  only the SDK external-field detector; it never starts a poller, listener,
  transmitter or replay operation. Field events carry UTC/local epochs,
  monotonic time, duration, count and explicit metadata describing the
  limitation.
- Added durable RF per-event storage with staged writes, immutable event IDs,
  storage reserve handling, ACK receipts and recovery after interrupted
  replacement. ACK receipts retain scalar time/fingerprint/family evidence and
  omit pulse arrays. New records no longer grow the legacy JSONL mirror.
- Aligned the BLE pull implementation with the C FAP: `DedSec` advertisement,
  service UUIDs, paged manifests, 80-byte reads, offset checks, CRC checks,
  durable import and ACK validation. Analyzer BLE sync now calls the durable
  adapter API and refreshes structural families after import.
- Extended the desktop analyzer with NFC metadata in details/CSV, timestamp
  normalization, structural grouping and source filters. Capture IDs are
  validated before they can be used as filesystem paths.
- Added fragmented wire tests, NFC contract tests, analyzer sync tests and a
  native C persistence harness that compiles the production store/settings
  sources with fault injection.

## Deliberate limits and unfinished work

- The external-FAP SDK exposes NFC carrier presence, not a passive UID or
  protocol listener. UID/raw exchanges therefore remain empty by design.
- Sub-GHz is a narrowband receiver. The analyzer waterfall is sampled RSSI and
  burst timing, not continuous IQ or a calibrated spectrum sweep. Arbitrary
  frequencies and calibrated duty-cycle scheduling remain outside this FAP.
- Physical BLE import, retry interruption and power-cut recovery still need a
  run on a Flipper containing a populated journal. The attempted `ufbt launch`
  check reported that no Flipper was connected, so the repository contains
  protocol and fault tests but no claim of hardware E2E validation.
- Existing pre-feature `events.jsonl` files are retained for evidence. A
  one-time migration/archive tool and a packaged desktop analyzer are still
  future work.

## Verification

From the repository root:

```text
python -m pytest -q tests uplink/tests
```

The current result is **47 passed**. Both Flipper applications build with the
Unleashed SDK and report **Target: 7, API: 88.9**:

```text
cd apps/rf_signal_hunter; python -m ufbt
cd apps/dedsec_uplink;    python -m ufbt
```

The generated FAPs are in `apps/rf_signal_hunter/dist/` and
`apps/dedsec_uplink/dist/`. A device install is intentionally not reported as
complete until a physical Flipper is detected by `ufbt launch`.
