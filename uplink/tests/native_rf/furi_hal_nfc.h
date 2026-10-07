#pragma once
/* Host stand-in: only the passive field-detector part of the NFC HAL.  No
 * poller, listener or field-on function is declared. */
#include <stdbool.h>
typedef enum {
    FuriHalNfcErrorNone,
    FuriHalNfcErrorBusy,
    FuriHalNfcErrorCommunication,
    FuriHalNfcErrorOscillator,
} FuriHalNfcError;
FuriHalNfcError furi_hal_nfc_acquire(void);
FuriHalNfcError furi_hal_nfc_release(void);
FuriHalNfcError furi_hal_nfc_low_power_mode_start(void);
FuriHalNfcError furi_hal_nfc_low_power_mode_stop(void);
FuriHalNfcError furi_hal_nfc_field_detect_start(void);
FuriHalNfcError furi_hal_nfc_field_detect_stop(void);
bool furi_hal_nfc_field_is_present(void);
