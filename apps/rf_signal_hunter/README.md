# RF Signal Hunter

This is the passive Scout vertical slice from `RF_SIGNAL_HUNTER_SPEC.md`.
It starts the Flipper asynchronous Sub-GHz receiver at 433.920 MHz, counts
pulse activity, gives immediate audiovisual feedback, and appends timestamped
pending event records to `apps_data/rf_signal_hunter/events.jsonl`. The desktop
side has a tested event model in `uplink/uplink/rf_hunter.py`.

The app has no transmit or replay path. Frequency profiles, device/session UUIDs,
BLE chunk upload, NFC observations, and detailed capture decoding are the next
adapters; the current UI intentionally labels the implementation as passive
Scout rather than claiming continuous SDR coverage. The current event IDs are
boot-local placeholders until the persistent identity adapter is added.
