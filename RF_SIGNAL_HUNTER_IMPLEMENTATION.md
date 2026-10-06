# RF Signal Hunter implementation status

This file records the implementation state against `RF_SIGNAL_HUNTER_SPEC.md`.
The repository is being built in small, testable vertical slices. The hardware
constraints in the specification are binding: the Flipper Sub-GHz radio is a
narrowband receiver, so the desktop waterfall represents sampled events rather
than continuous IQ.

## Implemented

- A standalone `apps/rf_signal_hunter` FAP builds with Unleashed SDK API 88.9.
- Scout/Capture/Follow/NFC observation screens are present. The Sub-GHz path is
  RX-only; NFC uses only the HAL external-field detector. No RF/NFC TX, replay,
  emulation, poller, or listener calls are used.
- Scout cycles the initial 315 MHz, 433.920 MHz and 868.350 MHz profiles,
  records event-scoped RSSI min/average/max and pulse timing data, and gives
  audiovisual feedback on the first observed edge before the burst closes.
- Device identity is persisted on the SD card and each run gets a session ID.
  Event IDs include device, session and sequence components.
- Captures use a bounded ISR-safe timing ring and durable SD records. Writes are
  staged/synchronised before the final event record is made visible.
- Device time/storage settings are available with **Up** on the main RF screen.
  RTC timezone offset (15-minute steps), reserve space and retention policy are
  saved in a versioned SD record. Events include normalized UTC calendar/epoch,
  the raw RTC epoch, the offset and a monotonic session-relative time.
- The same settings page exposes dwell (50 ms), RSSI threshold, capture window,
  silence threshold and the ALL/433/315/868 band profile. Values are range
  checked before saving and are applied to the live Scout/Capture loop. A
  feedback toggle can silence future audiovisual alerts without stopping logs;
  settings version 2 is migrated to the enabled default.
- The store refuses to replace an existing event ID. A low-space guard reports
  an error and red LED; the explicit stop-on-full policy halts Sub-GHz RX.
  Incomplete writes and an interrupted ACK/reclaim are recovered on restart.
- The RF Hunter BLE profile has separate service/characteristic UUIDs. The
  desktop pull protocol supports hello, paged manifest, offset reads, CRC checks,
  durable import, resumable staging and acknowledgement-based reclamation.
- Manifest requests are flow-controlled one item at a time; ACK is idempotent
  after a lost response, and the hello response reports pending count, free
  bytes and the negotiated chunk ceiling.
- Desktop event storage is idempotent by event ID. Repeated observations remain
  separate records while structural grouping is calculated independently.
- Desktop fingerprints use carrier, modulation, bandwidth, pulse widths/gaps,
  preamble/frame/repetition features and conservative complete-link grouping.
  Payload bytes and RSSI are retained as evidence, not used as device identity.
- NFC field-on/field-off intervals are persisted as timeline events with RTC
  UTC/local epochs, monotonic session offset, 13.56 MHz carrier, explicit
  technology/protocol metadata, duration and event count. Repeated NFC field
  observations group by this passive metadata while preserving each event.
- The Tk analyzer supports multiple stores, date/frequency/RSSI/source/family
  filters, sampled waterfall, spectrum, timeline, similarity explanations,
  family evidence details, nearest-observation reasons, background BLE sync
  progress, notes/location, raw capture details and versioned JSON/CSV export.
- Tests cover terminal behavior, RF event storage, upload resume/checksums,
  BLE framing, grouping, analyzer filters and session views.

## Deliberate limitations and remaining work

- NFC remains intentionally observation-only: the API exposes external carrier
  presence, not a passive protocol/UID sniffer for this FAP. UID and raw
  exchanges are therefore left blank rather than obtained by transmitting or
  pretending to decode unavailable data.
- The band list is intentionally limited to the three narrowband profiles
  available in this FAP (315, 433.920 and 868.350 MHz). Arbitrary user-defined
  frequencies and a calibrated duty-cycle scheduler are not exposed because
  the SDK receiver is narrowband rather than an IQ scanner.
- RSSI is sampled from the receiver and stored with events, but a full sweep
  spectrum snapshot and calibrated RSSI model are not available through the
  narrowband API.
- Follow currently uses the stored timing/frequency structural profile. A richer
  similarity profile editor and long-running background Follow service remain.
- The BLE profile and desktop adapter are implemented and covered by fragmented
  C-wire tests, including CRC/ACK rejection and resumable chunks. A hardware
  run with a populated event journal and a deliberately interrupted transfer is
  still pending because the Flipper was not visible to `ufbt launch` in this
  environment.
- The analyzer is a local Tkinter investigation console. A packaged desktop
  release, richer family graph animation and multi-project collaboration are
  still pending.
- Time/storage controls and reset recovery are implemented and tested with the
  production C code on a host storage facade. ACK receipts retain scalar
  identity/time/fingerprint metadata and omit raw arrays; new events no longer
  grow the legacy `events.jsonl` mirror. Hardware power-cut validation and
  migration of existing legacy journals remain; see
  [docs/RF_PERSISTENCE.md](docs/RF_PERSISTENCE.md).

## Verification

The RF Hunter FAP builds with
`python -m ufbt` and reports `Target: 7, API: 88.9`. Changes are committed and
pushed to `origin/main` after each completed slice.

The Python suite currently passes with `53 passed` when run as
`PYTHONPATH=uplink python -m pytest -q tests uplink/tests`. The native persistence test
compiles the actual `rf_store.c` and
`rf_settings.c`; injected write/sync failures and interrupted rename/ACK
sequences verify that pending records survive and settings recover. Run it with
`python -m pytest uplink/tests/test_rf_native_reliability.py -q` (one host test,
three groups of fault scenarios; passed on Windows/MSVC 2022).
