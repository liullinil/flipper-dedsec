from pathlib import Path


def _hunter_source() -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "apps" / "rf_signal_hunter" / "rf_signal_hunter.c").read_text(encoding="utf-8")


def test_nfc_mode_is_external_field_only_and_never_transmits():
    source = _hunter_source()
    assert "furi_hal_nfc_field_detect_start" in source
    assert "furi_hal_nfc_field_is_present" in source
    assert "nfc_technology\\\":\\\"external-field" in source
    for forbidden in (
        "furi_hal_nfc_poller_field_on",
        "furi_hal_nfc_poller_tx",
        "furi_hal_nfc_listener_tx",
        "nfc_poller_start",
        "nfc_listener_enable_rx",
    ):
        assert forbidden not in source


def test_nfc_event_contains_timeline_and_source_metadata():
    source = _hunter_source()
    for field in (
        "captured_at_utc",
        "captured_at_unix",
        "rtc_local_unix",
        "monotonic_ms",
        "nfc_field_duration_ms",
        "nfc_field_count",
        "carrier-presence",
    ):
        assert field in source
