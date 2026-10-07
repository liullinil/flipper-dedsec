"""RF Hunter inside DedSec Uplink must stay passive: receive and field detection only."""
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "apps" / "dedsec_uplink"


def _sources() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(APP.glob("*.c")))


def test_app_never_transmits_rf_or_nfc():
    source = _sources()
    for forbidden in (
        "furi_hal_subghz_tx",
        "furi_hal_subghz_start_async_tx",
        "furi_hal_subghz_write_packet",
        "subghz_devices_start_async_tx",
        "subghz_devices_write_packet",
        "furi_hal_nfc_poller_field_on",
        "furi_hal_nfc_poller_tx",
        "furi_hal_nfc_listener_tx",
        "nfc_poller_start",
        "nfc_listener_alloc",
    ):
        assert forbidden not in source, forbidden


def test_nfc_is_external_field_detection_only():
    engine = (APP / "rf_engine.c").read_text(encoding="utf-8")
    assert "furi_hal_nfc_field_detect_start" in engine
    assert "furi_hal_nfc_field_is_present" in engine


def test_nfc_record_describes_its_limits():
    record = (APP / "rf_record.c").read_text(encoding="utf-8")
    for field in ("captured_at_utc", "captured_at_unix", "rtc_local_unix", "monotonic_ms",
                  "nfc_field_duration_ms", "nfc_field_count", "external-field",
                  "carrier-presence"):
        assert field in record, field
