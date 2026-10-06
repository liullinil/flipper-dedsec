# RF Hunter desktop analyzer

The analyzer is a local, offline investigation console. It accepts one or more
Imported event-store directories (each containing legacy `events.jsonl` or the
API-88.9 per-event `events/<event_id>.json` journal plus optional
`captures/`), merges them by immutable `event_id`, and keeps every observation
even when observations belong to one signal family. Compact
`receipts/<event_id>.ack` records are also loaded as uploaded observations when
the FAP has reclaimed the raw event file. A copied Flipper journal can
therefore be opened directly; the BLE importer remains available for live
sync and durable ACK/retry handling.

Run the Tkinter UI from the repository root with the source package on
`PYTHONPATH`:

```powershell
$env:PYTHONPATH = "uplink"
python -m uplink.rf_analyzer C:\path\to\flipper\apps_data\rf_signal_hunter
```

On Windows, `rf_hunter_desktop.pyw` is the no-console launcher for the same
window. It can be packaged with the companion using PyInstaller when a desktop
release is prepared.

The UI provides:

- event and family filters by source, family ID, and text;
- live BLE import with background progress and durable ACK handling;
- sampled RSSI waterfall, frequency spectrum, and exact UTC timeline views;
- family observation counts, frequency/modulation summaries, time-of-day/RSSI
  evidence, source hypotheses, and selected-event details;
- structural similarity explanations based on carrier, modulation, pulse timing,
  and repetition pattern, including a nearest-observation reasons panel;
- JSON and CSV export of the active filtered view;
- multiple Flipper roots with transport deduplication by `event_id`.

The same engine is usable without a display:

```powershell
python -m uplink.rf_analyzer C:\path\to\store --export-json observations.json
python -m uplink.rf_analyzer C:\path\to\store --export-csv observations.csv
```

The waterfall is a visualization of timestamped event samples and RSSI values.
It is not continuous IQ or a direction finder. Similarity is provisional and
keeps the original event IDs visible so later desktop grouping can split or
merge families without discarding evidence.
