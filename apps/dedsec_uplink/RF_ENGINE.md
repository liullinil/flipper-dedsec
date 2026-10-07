# RF engine

Passive RF logger embedded in DedSec Uplink (integration contract v1). `uplink.c` owns the
UI and the BLE link and talks to the engine only through `rf_engine.h`. The engine never
transmits: no Sub-GHz TX, no NFC poller, listener or field-on call.

| File | Role |
| --- | --- |
| `rf_engine.c/.h` | Public API, worker thread, radio/NFC control, burst detection, feedback |
| `rf_capture.c/.h` | Lock-free ring between the TIM2 capture ISR and the worker |
| `rf_record.c/.h` | Record JSON (schema 1) and the coarse local signal shape |
| `rf_store.c/.h` | SD journal, crash recovery, CRC-32 |
| `rf_proto.c/.h` | `RL`/`RR`/`RA` request handler |

## Threads

- **Callers** (any Uplink thread): the setters only change mutex-protected control fields and
  post one coalesced wake message; `rf_engine_request()` copies the line into the worker's
  queue without blocking (depth 8; a full queue drops the line and the PC retries).
  `rf_engine_get_status()` and `rf_engine_status_line()` copy a snapshot.
- **TIM2 ISR** (`rf_capture_isr`): appends one packed timing (bit 31 level, 31-bit microseconds)
  to a 1024-entry ring and records the tick of the last edge. Overflow is counted and logged.
- **Worker** (`RfEngine`, 4 KiB stack): every HAL call, journal read/write, PC request,
  notification and callback. It wakes on the queue, every 5 ms while Sub-GHz RX runs,
  every 10 ms in NFC mode and every 250 ms otherwise.

Callbacks run on the worker. `changed` is rate limited to one call per 100 ms (trailing call
guaranteed). No callback runs after `rf_engine_free()` has been entered. Do not call
`rf_engine_free()` from a callback or while holding a lock that a callback takes.

## Radio sequence

All on the worker:

1. First start: `furi_hal_subghz_reset()` → `idle()` →
   `load_custom_preset(subghz_device_cc1101_preset_ook_650khz_async_regs)` (async OOK,
   650 kHz RX filter, GDO0 = demodulated data).
2. Every tune or Scout hop: `stop_async_rx()` (if running) → `idle()` →
   `set_frequency_and_path(f)` → `flush_rx()` → `start_async_rx(rf_capture_isr, ring)`.
   `set_frequency()` calibrates and `furi_check()`s for IDLE, so it never runs during RX.
3. Stop, mode change to NFC and exit: `stop_async_rx()` (if running) → `sleep()`.

The NFC HAL keeps SPI bus R, which the CC1101 shares, from `furi_hal_nfc_acquire()` until
`furi_hal_nfc_release()`. The CC1101 is therefore asleep before NFC is acquired and is only
touched again after release, and acquire/release happen on the same thread. NFC mode uses
`acquire` → `low_power_mode_stop` → `field_detect_start`, polls `field_is_present()` every
10 ms and undoes it in reverse order. A busy NFC HAL is retried once per second.

Frequencies: 315.000, 433.920 and 868.350 MHz. `band` limits Scout hopping and the Capture
frequency. Scout hops every `dwell_ms` while no burst is open. Capture uses the frequency of the
last event if the band allows it, otherwise 433.92 MHz first. Follow uses the frequency of its
profile and ignores `band`.

## Bursts and events

- **Noise floor:** every frequency keeps the lower envelope of its RSSI samples outside bursts
  (falls with 1/8 of a quieter sample, rises with 1/64 of a louder one) and may trigger only after
  8 samples. Stopping the receiver forgets the floors.
- **Trigger:** an RSSI sample (every 5 ms) at or above max(`rssi_threshold_dbm`, floor + 8 dB);
  the level is kept for the whole burst. The event starts with the pre-trigger timings of the
  last 10 ms (at most 64). The pre-trigger is cleared, so it never leaks into the next event.
- **Signal or noise:** a burst counts as a signal once it has 16 timings with its first and
  last strong RSSI samples at least 10 ms apart, or strong samples 50 ms apart (a carrier without
  edges). Only signals are recorded, counted and give feedback; a spike that crossed the trigger
  for one sample is dropped.
- **End:** a space longer than `silence_us`; or no edge for `silence_us` while RSSI is below the
  trigger; or no RSSI sample above the trigger for max(150 ms, `silence_us`), for noisy
  channels where the demodulator keeps toggling; or `capture_ms` elapsed.
- **Record:** up to 512 timings are kept in RAM and written while they fit the 4 KiB record
  buffer; `pulse_count` counts every timing of the event. `duration_us` is the sum of the
  in-burst timings, or the time above threshold for a carrier without OOK edges. RSSI
  min/avg/max come from the samples taken during the burst. `modulation` is `"OOK"`
  (receiver demodulator).
- **Families:** a coarse shape key (frequency + log2 pulse-width histogram bands holding at
  least 15 % of the timings + dominant band) is the `fingerprint_id`; `families` counts distinct
  keys (last 64 remembered).
- **Follow:** every recorded non-Follow event becomes the profile (frequency + histogram). In
  Follow mode, bursts on another frequency or with similarity below 0.70
  (0.8 × histogram overlap + 0.2 × timing-count ratio) are ignored. A match is recorded with
  `follow_profile_id` and `follow_similarity` and gives the double pulse. Without a profile,
  Follow records like Capture and the first event becomes the profile.
- **NFC:** a field event starts at field-on and ends after 1 s without a field (merges a
  reader's polling bursts). `nfc_field_count` is the session's count of field-on detections.
- **Feedback** (`feedback`): one 50 ms vibro + cyan LED pulse as soon as a burst proves to be
  a signal (NFC: at field-on), at most once per second; Follow matches give a double pulse. While
  events are unseen the app (`uplink.c`) blinks the LED cyan for 25 ms every 4 s; looking at the RF
  tab marks them seen. A failed journal write
  gives a 50 ms red blink (at most every 5 s, regardless of `feedback`) and increments
  `errors`; `storage_full` is set while the reserve is reached.

Records are the schema-1 JSON of the former standalone app, one object per file ending in
`}\n`. Event id: `rf-<device_id>-<session_id>-<sequence>` (at most 46 characters).
`captured_at_unix` = RTC calendar epoch − `tz_offset_minutes` × 60 (clamped at 0).

## Journal

`/ext/apps_data/dedsec_uplink/rf/` (absolute paths only):

| Path | Content |
| --- | --- |
| `events/<id>.json` | Pending records |
| `events/<id>.bad` | Quarantined malformed records (never listed) |
| `uploaded/<id>.json` | Records kept after an ACK when `keep_uploaded` is set |
| `carry/<pc_id>/<id>.json` | Records PC `<pc_id>` put here for another PC (`RP`/`RW`) |
| `device_id` | 16 hex characters, created once |
| `inflight` | 64-byte intent marker: `W <id>` (writing) or `M <id>` (moving) |

On this firmware `storage_common_rename()` is "remove destination, copy, remove source", so the
journal never renames a record:

- **Save:** check free space (1 MiB reserve; uploaded copies are pruned first when low) →
  rewrite `inflight` = `W <id>` → create `events/<id>.json` with `FSOM_CREATE_NEW` (ids are
  immutable) → write → sync.
- **ACK:** stream the record through CRC-32 and compare size + crc → delete it, or with
  `keep_uploaded` rewrite `inflight` = `M <id>` → copy to `uploaded/` → sync → delete the
  pending file. A repeated ACK of a record that is gone is answered with `RK` when it matches
  one of the last 8 ACKs or a kept copy.
- **Start (recovery):** read `inflight`. `W <id>`: if `events/<id>.json` is empty or does not
  end with `}\n`, it is a torn write and is removed. `M <id>`: if the pending file still exists,
  the copy in `uploaded/` is partial or a duplicate and is removed; the record stays pending.
  A torn `inflight` (no newline, or naming an intact record) changes nothing. Then
  `events/` and `uploaded/` are counted once; afterwards the counts are kept in memory.
- **Listing:** `RL` streams the record to compute size and crc32. A file that is not one
  complete JSON object is renamed to `<id>.bad` so it cannot block every sync round.
- `uploaded/` keeps at most 512 copies (directory order, roughly oldest first).
- **Carrying:** `RO|pc_id` tells the store which PC is on the link. `RP` opens
  `carry/<pc_id>/<id>.json` (`RH` when the same record is already pending or carried, a refusal
  when another record has this id), `RW` chunks must arrive in order and are answered with the
  bytes stored; at the announced size the CRC-32 and the `{` ... `}` + newline shape are checked and
  the file is synced. A record left unfinished (3 s without requests, another `RP`, a different
  PC) is deleted. `RL` lists `events/` first, then the other PCs' `carry/` folders, and only to a
  PC that sent `RO`; `RR`/`RA` find a record in either place. A carried copy that is not one
  complete record is deleted instead of quarantined: the PC that brought it still has it.
- One read handle is cached for the record being synced (closed after 3 s without
  requests, before any remove), so the `RR` chunks of a record do not each rescan the folder.

`R|listed|stored|free_kb|state|errors|carry`: `listed` = what the PC on the link can import
(`events/` plus the other PCs' carried records), `stored` = files in `events/` + `uploaded/` +
`carry/`, `carry` = carried records of every PC; `state` 0 off, 1 Sub-GHz RX, 2 NFC detect. The engine never sends `R|` itself; it calls `changed` when
`pending` (or anything else) changes and the app sends the line. `RX|off` is for the app to
send when RF sync is disabled; the engine only produces `nf`, `bad` and `io`. The text of an
`RX` line starts with the event id when the request had a valid one.

## Limits

- Heap ≈ 19 KiB: ring 4 KiB, timings 2 KiB, record buffer 4 KiB, store ≈ 2.5 KiB,
  queue ≈ 2.4 KiB (eight requests up to an `RW` line), worker stack 4 KiB, engine state ≈ 1 KiB.
- The engine is narrowband: one frequency at a time, OOK demodulation only.
- A channel that stays above the threshold produces one event per `capture_ms`.
- FAT directory lookups are linear: saving and listing slow down with thousands of pending
  files. Every small file also takes a whole cluster on the card.

## Verified and unverified

Verified on the host by `uplink/tests/test_rf_native_reliability.py`, which compiles the
production C files:

- Journal (`rf_store.c`) against a storage fake that copies on rename, reuses directory
  slots, refuses to remove open files and can cut power mid-write: CRC-32 = `binascii.crc32`,
  power cuts during record writes, marker rewrites and keep-moves, write/sync/remove failures,
  quarantine, low space, the 512-copy cap.
- Records (`rf_record.c`) from edge-case inputs: `json.loads`, UTC/epoch agreement, timing cap.
- Protocol (`rf_proto.c`): the full `RL`/`RR`/`RA` sync loop with pipelined reads, malformed
  lines, mismatching ACKs and repeated ACKs before and after a restart.
- The whole engine (`rf_engine.c` + `rf_capture.c`) in a deterministic fake world: scripted
  OOK transmissions, noise and NFC fields drive the real worker loop, and the fake HAL aborts
  on every call order the firmware would `furi_check()` or deadlock on (tuning during RX,
  RX without preset, stop without RX, sleep during RX, CC1101 use while NFC holds SPI bus R,
  HAL calls outside the worker). Scenarios: Scout hop timing, burst segmentation and
  per-event pulse counts, threshold, noise, NFC merge and busy retry, Follow match/ignore,
  storage full, back-to-back capture windows (no pre-trigger leak), the gap rule, rapid
  mode/run/config changes and recording at exit. Mutating the tune order, the preset, the
  sleep, the pre-trigger reset, the gap rule or adding a HAL call to a setter makes it fail.
  The fake declares no TX/poller/listener/field-on function, so the engine only builds there
  while it stays passive.

The engine builds with `-Werror` against Unleashed unlshd-093 (API 88.9); a probe FAP that
calls every API function links and passes APPCHK (81 imported symbols, none for TX). The
largest static stack frames on the worker path add up to about 1.2 KiB before firmware
callees.

Not verified without a Flipper: the real radio (preset behaviour, RSSI levels, OOK noise edge
rates, the 5 ms/150 ms detection timing and thresholds), the real NFC field detector, power use,
the worker's actual stack high-water mark, SD timing, and the feel of the feedback patterns.
