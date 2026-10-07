"""The Flipper app and the companion are released together under one tag.

The companion compares the latest release tag with COMPANION_VERSION to offer its own update,
and the Flipper app compares it with UPLINK_VERSION, so all of them must move together.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_versions_match():
    flipper = re.search(r'#define UPLINK_VERSION "([\d.]+)"', read("apps", "dedsec_uplink", "uplink_ota.h"))
    companion = re.search(r'COMPANION_VERSION = "([\d.]+)"', read("uplink", "uplink", "updater.py"))
    fap = re.search(r'fap_version="([\d.]+)"', read("apps", "dedsec_uplink", "application.fam"))
    assert flipper and companion and fap
    assert flipper.group(1) == companion.group(1)
    assert flipper.group(1).startswith(fap.group(1) + ".")
