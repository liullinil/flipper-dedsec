# RF time and storage implementation

## Time

The Flipper RTC exposes calendar fields without timezone metadata. The RF app
therefore persists the UTC offset the user assigns to those fields. Set it with
**Up → RTC UTC±hh:mm**; Left/Right changes it in 15-minute steps. The initial
value is UTC+00:00. Set the actual local offset before recording if the Flipper
clock is local time. This fixed offset does not infer daylight-saving changes.

Each record now carries `captured_at_utc`, `captured_at_unix`,
`timezone_offset_minutes`, `rtc_local_unix` and `monotonic_ms`. The UTC epoch is
the RTC-calendar epoch minus the configured offset. Monotonic time starts at
this application session, so changing the RTC does not change that ordering
evidence. The hardware calendar has whole-second precision; the app does not
invent subsecond RTC accuracy. Desktop import preserves both epochs and the
offset, normalizes ISO timestamps to UTC and rejects an explicit epoch that
disagrees with the calendar timestamp before acknowledgement. An offset-free
legacy timestamp uses its recorded device offset.

## Retention

The time/storage page exposes three policies:

| Policy | After ACK | At the reserve-space guard |
| --- | --- | --- |
| ACK → compact | Keep ACK receipt; reclaim per-event capture JSON | Report storage error and red indicator; do not overwrite pending records |
| Keep ACK capture | Keep per-event capture JSON plus receipt | Report storage error and red indicator; do not overwrite pending records |
| Stop when full | Keep receipt; reclaim per-event capture JSON | Stop Sub-GHz RX and show the storage error |

The free-space reserve defaults to 32 KiB and is editable in 16 KiB steps. Its
purpose is to leave room for ACK/settings metadata; it is not a quota that can
discard old pending events. An existing event ID is immutable and cannot be
overwritten by a new call to save.

Settings use a versioned binary record compatible with the original settings
prefix. A temporary write is synced before replacement; a backup lets startup
recover the previous settings when reset interrupts replacement. Partial event
writes are uncommitted and removed at startup. Backup event records from an
interrupted replacement are restored when no committed record is present.
ACK receipts are compact JSON containing event identity, timestamp and available
fingerprint/family/scalar capture metadata. Raw pulse arrays are omitted. They
are synced and renamed before capture reclamation; interrupted reclamation
resumes on startup using the receipt as the commit marker. Newly recorded
events have only one raw copy under `events/`; the app no longer appends a
second copy to `events.jsonl`.

## Verification and remaining work

`uplink/tests/test_rf_native_reliability.py` compiles the production C settings
and store implementations with a faultable storage facade. It verifies settings
round-trip, sync failure, reset recovery, low-space rejection, pending-event
preservation, ACK sync failure, unknown-ACK rejection, duplicate-ID rejection,
retention selection and recovery after interrupted replacement/reclamation.
The same source builds as FAP API 88.9.

Hardware power-cut validation has not been performed. Existing legacy
`events.jsonl` files are deliberately left untouched because they may contain
observations created before the per-event store was introduced. A one-time
legacy import/archive flow remains; these historical files will not grow.
Receipt summaries retain a fingerprint/family when present in the event; the
local Scout/Follow grouping work must produce those fields consistently.
The native test verifies compact receipt content and exclusion of pulse arrays.
Desktop time conversion and epoch consistency are covered by
`uplink/tests/test_rf_time.py`.
