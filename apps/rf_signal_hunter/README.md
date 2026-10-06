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
- **NFC** is an explicit safe status screen. External FAPs do not have a stable
  SDK 88.9 NFC poller API, so this mode does not enable an NFC field or claim
  observations.

A stable random device ID is persisted in `apps_data/rf_signal_hunter/device_id`; each launch gets a random session ID, so event IDs remain unique across restarts. Events are appended to `apps_data/rf_signal_hunter/events.jsonl` with RTC
calendar time, monotonic tick, session/sequence IDs, mode, frequency, pulse
count, and last pulse duration. Back exits the app and always stops RX before
returning. The implementation is intentionally narrowband and does not claim
continuous SDR coverage.
