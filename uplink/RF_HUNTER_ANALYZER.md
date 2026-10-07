# RF Hunter: companion sync and analyzer

RF Signal Hunter is an **RF tab of the DedSec Uplink Flipper app** and an **RF analyzer window of the
DedSec Uplink companion**. There is no separate RF Hunter BLE device and the analyzer never opens a
BLE connection: journal records travel over the companion's existing Uplink link. The binding
protocol is `docs/rf_integration_contract.md` (v1). Everything is passive; nothing here transmits.

| Piece | Module | Thread |
|---|---|---|
| Import engine | `uplink/uplink/rf_sync.py` (`RfSync`) | driven by the link |
| Event store | `uplink/uplink/rf_hunter.py` (`EventStore`, `RfEvent`) | owned by `RfSync` |
| Families / similarity | `uplink/uplink/rf_fingerprint.py` | UI thread |
| Analyzer window and CLI | `uplink/uplink/rf_analyzer.py` (`AnalyzerWindow`, `main`) | companion UI thread |

## Where the data lives

* **Companion store** (written only by `RfSync`): `%LOCALAPPDATA%\DedSecUplink\rf_hunter`
  (`default_store_root()`), holding `events.jsonl` (one record per line) and
  `captures/<event_id>.bin`, the exact bytes received from the Flipper.
* **Flipper journal** (SD card): `/ext/apps_data/dedsec_uplink/rf/` with `events/<event_id>.json`
  (pending upload) and `uploaded/<event_id>.json` (kept after an ACK when "keep uploaded" is on).
  The engine uses this absolute path because `/data` resolves per calling thread.
* To inspect a card without the link, copy `apps_data/dedsec_uplink/rf` (or the whole card) to the PC
  and use **Open folder…**. The analyzer accepts the `rf` folder, its `events/` or `uploaded/`
  sub-folder, `apps_data/dedsec_uplink`, and the card root. Opened folders are read-only.
* Journals of the former standalone app are still readable: its `APP_DATA_PATH("rf_signal_hunter")`
  resolved to `/ext/apps_data/rf_signal_hunter/rf_signal_hunter/` (`events/` and `receipts/*.ack`);
  opening `apps_data/rf_signal_hunter` finds it.

## Import over the Uplink link (`RfSync`)

```python
from uplink.rf_sync import RfSync, default_store_root

sync = RfSync(default_store_root())   # creates the folder if needed
# link thread:  for every Flipper line -> sync.handle_line(line.split("|"))  (True = RF line)
#               every 20-50 ms         -> send each line of sync.urgent_lines()
# link status:  sync.on_link(True / False)
# UI thread:    sync.status(), sync.sync_now()
```

`RfSync(store_root, *, clock=time.monotonic, wall_clock=time.time, send_clock=True)` is thread-safe:
`handle_line` may run on the BLE thread, `urgent_lines` on the link loop and `status`/`sync_now` on
the UI thread. Store I/O (the fsync before an ACK) runs inside `urgent_lines` without holding the
lock, so the BLE callback and the UI never wait for the disk. `sync.store` is the `EventStore` it
writes; readers should open their own read-only `EventStore` on `sync.store_root` (the analyzer does).

Protocol (PC → Flipper `RL|cursor`, `RR|event_id|offset`, `RA|event_id|size|crc32`, `Z|utc|offset`;
Flipper → PC `R|…`, `RI|…`, `RE`, `RD|…`, `RK|…`, `RX|code|text`):

* A round starts when `R|pending` is above 0 or on `sync_now()`. One `RL` is outstanding at a time.
* A record is read with up to four `RR` in flight, matched by offset. A short reply leaves a gap that
  is requested at once; a missing reply is requested again after 3 s; the record is given up after
  20 s. Records of any size (also above 768 bytes) are read completely by offset.
* Size and CRC-32 (`binascii.crc32`, unsigned) are verified. A mismatch downloads the record once
  more, then rejects it.
* The record is committed durably (capture blob and JSONL line flushed and fsynced) **before** `RA`;
  the round waits for `RK`, which removes the record from `events/`, so the cursor stays the same.
* A record that cannot be imported (malformed JSON, invalid fields, identity mismatch, conflict,
  timeout, rejected ACK) is skipped with `cursor = next`, counted in `failed`, reported in
  `last_error` and stays on the Flipper. Unchanged rejected records are not downloaded again by
  automatic rounds; **Sync now** retries them.
* Already imported: if the stored capture matches size and CRC the record is acknowledged without a
  download; if the stored event has no capture (for example a store built from a copied journal)
  the record is downloaded, attached durably and then acknowledged; a different payload for the
  same ID is a conflict and is **never** acknowledged.
* `RX|off` (sync disabled on the Flipper), a cursor that does not advance and a failed write on the
  PC abort the round and pause automatic rounds for 30 s. A link drop aborts the round cleanly; the
  next link starts over (already committed records are then only acknowledged).
* `RX` carries no request id; replies arrive in request order, so late replies to reads that are no
  longer needed are recognized and never taken as the answer to a later `RL` or `RA`.
* `Z|utc_unix|tz_offset_min` (the PC clock and local UTC offset) is sent first on every link and
  hourly while connected; pass `send_clock=False` if the companion sends it itself.

`status()` returns `pending`, `stored`, `free_kb`, `state` (0 off, 1 Sub-GHz RX, 2 NFC) and
`errors` from the last `R|` line (`None` until one arrived), the session totals `imported`, `failed`,
`skipped` (already imported, acknowledged again) and `conflicts`, plus `last_error` (kept until a
round finishes without failures), `syncing`, `last_sync` (epoch seconds), `link_up`,
`current_event`, `current_progress` and `store_root`.

## Event store guarantees (`EventStore`)

* Every write is durable before the call returns: data is written to a temporary file, flushed and
  fsynced, then renamed (Windows: `MoveFileEx` with write-through; POSIX: directory fsync). New
  records are appended to `events.jsonl` with an fsync; only a changed existing record rewrites
  the file, and unparseable lines are carried over instead of being dropped.
* Only changed records are written (dirty tracking). In a copied per-event journal untouched
  records keep their exact bytes.
* A malformed record (bad JSON, `Infinity`/`NaN`, an out-of-range epoch, wrong field types, an
  unsafe event ID or capture path) never prevents the store from opening: it is skipped and counted
  in `skipped_records`, with details in `load_errors`; the analyzer shows the count.
* `EventStore(root, read_only=True)` refuses all writes; the analyzer opens every folder this way.

## Analyzer window (`AnalyzerWindow`)

```python
from uplink.rf_analyzer import AnalyzerWindow, default_store_root

window = AnalyzerWindow(tk_root, default_store_root(), sync=sync, on_close=forget_window)
window.show()     # bring an existing window to the front
window.close()    # idempotent; cancels its timers, then calls on_close
window.alive      # False after close
```

`AnalyzerWindow(parent, store_root, sync=None, *, extra_roots=(), on_close=None, project=None)` builds
a `tk.Toplevel` on an existing Tk root; all calls must come from that root's thread (the companion's
single UI thread, shared with the settings window). It uses its own `RF.*` ttk style names and does
not change the theme of the shared interpreter.

* Reloads a folder when its journal changes (stat-only check every 3 s, immediately after `RfSync`
  reports a new import).
* Shows the `RfSync` status (pending on the Flipper, free space, receiver state, imported/failed,
  progress, last sync time, last error) and a **Sync now** button. With `sync=None` it is a viewer.
* **Open folder…** adds a copied journal read-only. Export JSON/CSV writes the filtered view;
  failures of user actions are shown in a dialog and logged.
* Filters: text, source, **MHz** (`433.92` matches ±0.2 MHz, `433.92+-0.05` sets the tolerance,
  `433-434` is a range), RSSI bounds and **From/To UTC** (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM[:SS]`;
  a date as the end means the whole day). An unreadable filter is reported and ignored.
* Views: sampled RSSI waterfall (newest event at the top; a click selects the row under the cursor),
  frequency spectrum, UTC timeline, family navigator with detail (time-of-day, RSSI, variants,
  hypothesis, raw/imported counts) and a nearest-observation panel with plain-language reasons.

Signal families are computed on the PC by complete-link structural grouping and kept in memory
(`project.family_key(event)`, `project.fingerprint_key(event)`). They are **never written into the
stored records**, so the Flipper's `local-…` fingerprint hint and `family_id` stay as recorded;
exports add `desktop_family_id` and `desktop_fingerprint_id` columns. Families are rebuilt only when
the set of events changes, not on filter or selection changes. Features are cached per record
content, candidate families are narrowed by frequency bucket and pulse-count ratio, and an exact
upper bound rejects most pairs before the full comparison; the result is identical to the naive
grouping (verified against the previous implementation). On the reference synthetic mix
(jittered remotes on three bands, noise bursts, NFC fields) a full rebuild takes about 0.04 s for
300 events, 0.22 s for 1000 and 0.7 s for 2000 events, down from 8.6 s, 59 s and 317 s (1000 events
in the reviewer's measurement: 122 s); a rebuild after a reload with mostly known records takes
about 0.05 s for 1000 events. With 1000 events the window opens in about 0.45 s, a refresh after a
filter or family change takes about 0.1 s and loading the store about 0.07 s; committing 1000
records durably takes about 1.8 s.

## Command line and standalone viewer

```powershell
$env:PYTHONPATH = "uplink"
python -m uplink.rf_analyzer                                  # viewer on the companion store
python -m uplink.rf_analyzer D:\sd-copy\apps_data\dedsec_uplink\rf
python -m uplink.rf_analyzer D:\sd-copy --export-json observations.json --export-csv observations.csv
```

`main(argv)` runs exports headless or opens the standalone viewer (`open_analyzer(store_dirs)`,
which creates its own Tk root in the calling thread and runs its main loop; imports are not
available there). `rf_hunter_desktop.pyw` is the no-console launcher. Under `pythonw` or a
windowed build `--help` and argument errors are shown in a dialog instead of crashing on a missing
console, and errors are logged to `%LOCALAPPDATA%\DedSecUplink\rf_hunter.log` (inside the companion
the analyzer logs to the companion log).

The waterfall is a visualization of timestamped event samples and RSSI values. It is not
continuous IQ or a direction finder. Similarity is provisional and keeps the original event IDs
visible so later grouping can split or merge families without discarding evidence.
