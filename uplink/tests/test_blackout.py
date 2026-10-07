"""Blackout decisions without Tk: the PIN, the Flipper's lines, auto-blackout timing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uplink import blackout as bo  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeUi:
    """Runs UI callables at once (the real one posts them to the Tk thread)."""

    def call(self, fn, *args):
        fn(*args)


class FakeDesktop:
    def __init__(self):
        self.log = []

    def hide_windows(self):
        self.log.append("hide")

    def mute(self):
        self.log.append("mute")

    def unmute(self):
        self.log.append("unmute")

    def restore_windows(self):
        self.log.append("restore")


class FakeScreen:
    built = 0

    def __init__(self, blackout):
        FakeScreen.built += 1
        self.closed = False

    def close(self):
        self.closed = True


def make(auto=False, pin="2468", clock=None):
    clock = clock or Clock()
    cfg = {"blackout_auto": auto, "blackout_pin": bo.hash_pin(pin) if pin else None}
    saved = []
    desktop = FakeDesktop()
    b = bo.Blackout(FakeUi(), cfg, saved.append, desktop=desktop, screen_factory=FakeScreen, clock=clock)
    return b, cfg, saved, desktop, clock


def test_pin_hashing_and_rules():
    stored = bo.hash_pin("1234")
    assert bo.check_pin(stored, "1234") and not bo.check_pin(stored, "1235")
    assert not bo.check_pin(None, "1234") and not bo.check_pin(stored, "")
    assert bo.check_pin({"salt": stored["salt"], "hash": stored["hash"]}, "1234")
    assert bo.pin_problem("12ab") == "digits only"
    assert bo.pin_problem("123") == "at least 4 digits"
    assert bo.pin_problem("1" * 13) == "at most 12 digits"
    assert bo.pin_problem("2468") is None


def test_flipper_locks_and_unlocks_the_pc():
    b, cfg, saved, desktop, clock = make()
    assert b.frame_line() == "BO|0|0"
    assert b.handle_line(["BO", "1"])
    assert b.active and b.reason == "flipper"
    assert desktop.log == ["hide", "mute"]
    assert b.frame_line() == "BO|1|0"
    assert b.handle_line(["BO", "1"]) and desktop.log == ["hide", "mute"]   # already up: nothing twice
    assert b.handle_line(["BO", "0"])
    assert not b.active and desktop.log == ["hide", "mute", "unmute", "restore"]
    assert not b.handle_line(["C", "1", "dir"])


def test_pin_attempts_slow_down_after_misses():
    b, *_ = make()
    b.lock("tray")
    assert b.try_pin("0000") == (False, 0.0)
    assert b.try_pin("0000") == (False, 0.0)
    ok, wait = b.try_pin("0000")
    assert not ok and wait == 1.5
    assert b.try_pin("2468") == (True, 0.0) and not b.active


def test_without_a_pin_enter_unlocks_and_the_screen_says_so():
    b, *_ = make(pin="")
    b.lock("flipper")
    assert b.try_pin("1234") == (False, 0.0) and b.active
    assert b.try_pin("") == (True, 0.0) and not b.active


def test_auto_blackout_arms_after_ten_minutes_and_waits_for_the_grace():
    clock = Clock(5000.0)
    b, cfg, saved, desktop, _ = make(auto=True, clock=clock)
    b.on_link("connected")
    assert b.auto_state() == bo.AUTO_ARMING and b.frame_line() == "BO|0|1"
    b.on_link("searching")                       # the Flipper walked away during the arming time
    clock.t += 60
    assert not b.tick()                           # not armed yet
    clock.t += bo.ARM_AFTER
    assert b.auto_state() == bo.AUTO_ARMED
    assert b.tick() and b.active and b.reason == "link lost"
    b.unlock("pin")
    # a short hiccup inside the grace never locks
    b.on_link("connected")
    b.on_link("searching")
    clock.t += bo.LINK_GRACE - 5
    assert not b.tick()
    b.on_link("connected")
    clock.t += 100
    assert not b.tick()
    # after the grace it does
    b.on_link("searching")
    clock.t += bo.LINK_GRACE
    assert b.tick() and b.active


def test_a_flipper_that_said_goodbye_or_a_paused_uplink_never_triggers_it():
    clock = Clock()
    b, *_ = make(auto=True, clock=clock)
    clock.t += bo.ARM_AFTER + 1
    b.on_link("connected")
    assert b.handle_line(["BB"])
    b.on_link("searching")
    clock.t += bo.LINK_GRACE * 2
    assert not b.tick() and not b.active
    b.on_link("connected")                        # a new session forgets the goodbye
    b.on_link("paused")
    clock.t += bo.LINK_GRACE * 2
    assert not b.tick()
    b.on_link("connected")
    b.on_link("searching")
    clock.t += bo.LINK_GRACE
    assert b.tick()


def test_auto_blackout_off_by_default_and_the_toggle_is_saved():
    b, cfg, saved, desktop, clock = make(auto=False)
    clock.t += bo.ARM_AFTER + 1
    b.on_link("connected")
    b.on_link("searching")
    clock.t += bo.LINK_GRACE * 2
    assert not b.tick()
    b.set_auto(True)
    assert saved and saved[-1]["blackout_auto"] is True
    assert b.tick() and b.active
    b.shutdown()
    assert not b.active and desktop.log[-2:] == ["unmute", "restore"]


def test_never_locks_before_a_link_ever_existed():
    """After a reboot the Flipper is not there yet: 'searching' from the start is not a loss."""
    clock = Clock()
    b, *_ = make(auto=True, clock=clock)
    clock.t += bo.ARM_AFTER + 1
    b.on_link("searching")
    clock.t += bo.LINK_GRACE * 10
    assert not b.tick()


def test_the_flipper_coming_back_unlocks_after_an_absence():
    clock = Clock()
    b, *_ = make(auto=True, clock=clock)
    clock.t += bo.ARM_AFTER + 1
    b.on_link("connected")
    b.on_link("searching")
    clock.t += bo.LINK_GRACE
    assert b.tick() and b.active and b.reason == "link lost"
    clock.t += 600
    b.on_link("connected")                        # back at the desk
    assert not b.active and b.frame_line() == "BO|0|2"
    # a blackout from the Flipper at the desk: a short BLE hiccup must not open it
    b.lock("flipper")
    b.on_link("searching")
    clock.t += 5
    b.on_link("connected")
    assert b.active
    # ... but coming back after a real absence does
    b.on_link("searching")
    clock.t += bo.LINK_GRACE
    b.on_link("connected")
    assert not b.active


def test_a_tray_blackout_while_the_flipper_is_away_opens_when_it_arrives():
    b, cfg, saved, desktop, clock = make()
    clock.t += 100                                # the link was never up
    b.lock("tray")
    b.on_link("connected")
    assert not b.active
