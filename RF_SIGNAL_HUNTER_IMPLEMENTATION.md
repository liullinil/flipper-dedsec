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
  records RSSI min/average/max and pulse timing data, and gives immediate
  audiovisual feedback at burst boundaries.
- Device identity is persisted on the SD card and each run gets a session ID.
  Event IDs include device, session and sequence components.
- Captures use a bounded ISR-safe timing ring and durable SD records. Writes are
  staged/synchronised before the final event record is made visible.
- The RF Hunter BLE profile has separate service/characteristic UUIDs. The
  desktop pull protocol supports hello, paged manifest, offset reads, CRC checks,
  durable import, resumable staging and acknowledgement-based reclamation.
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
  notes/location, raw capture details and JSON/CSV export.
- Tests cover terminal behavior, RF event storage, upload resume/checksums,
  BLE framing, grouping, analyzer filters and session views.

## Deliberate limitations and remaining work

- NFC remains intentionally observation-only: the API exposes external carrier
  presence, not a passive protocol/UID sniffer for this FAP. UID and raw
  exchanges are therefore left blank rather than obtained by transmitting or
  pretending to decode unavailable data.
- The initial Sub-GHz profile list and dwell schedule are compiled defaults;
  user-editable band profiles and threshold/duty-cycle settings remain to be
  added.
- RSSI is sampled from the receiver and stored with events, but a full sweep
  spectrum snapshot and calibrated RSSI model are not available through the
  narrowband API.
- Follow currently uses the stored timing/frequency structural profile. A richer
  similarity profile editor and long-running background Follow service remain.
- The BLE profile and desktop adapter are implemented, but full end-to-end RF
  import still needs a hardware run with a populated event journal and retry
  interruption test. The protocol is unit-tested with fragmented mock frames.
- The analyzer is a local Tkinter investigation console. A packaged desktop
  release, richer family graph animation and multi-project collaboration are
  still pending.
- Device timezone offset and retention policy are persisted in settings and
  applied to event timestamps; ACK receipts and storage-full behavior are
  durable and policy-controlled.

## Verification

The current Python suite passes with `29 passed`. The RF Hunter FAP builds with
`python -m ufbt` and reports `Target: 7, API: 88.9`. Changes are committed and
pushed to `origin/main` after each completed slice.
