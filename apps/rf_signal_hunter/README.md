# RF Signal Hunter

Passive RF observation FAP for Flipper SDK API 88.9. The app never calls a TX,
replay, or emulation API.

Use Left/Right to select a mode and OK to start or stop the receiver:

- **Scout** counts Sub-GHz bursts at 433.920 MHz and stores each observation.
- **Capture** uses the same RX callback and records pulse timing metadata for
  every detected burst, with immediate audiovisual feedback.
- **Follow** applies a lightweight duration fingerprint (+/-500 us) to the most
  recently observed burst and only notifies/stores matching bursts. The first
  match establishes the local profile.
- **NFC** acquires the SDK 88.9 HAL and enables only its external-field detector.
  A field-on/field-off interval is stored as a timeline event with RTC start
  time, monotonic session offset, duration, 13.56 MHz carrier, and explicit
  `external-field`/`carrier-presence` metadata. It never starts a poller or
  listener and never enables a carrier, sends a frame, or reads a UID.

A stable random device ID is persisted in `apps_data/rf_signal_hunter/device_id`; each launch gets a random session ID, so event IDs remain unique across restarts. Durable event records are committed under `apps_data/rf_signal_hunter/events/` and reclaimed only after a validated BLE ACK; receipts retain compact identity/time/fingerprint metadata. Records include RTC calendar time, UTC/local epochs, a monotonic session offset, session/sequence IDs, mode, frequency, pulse count, and last pulse duration where applicable. Back exits the app and always stops RX/NFC detection before returning. The implementation is intentionally narrowband and does not claim continuous SDR coverage.
