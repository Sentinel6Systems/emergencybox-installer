#!/usr/bin/env python3
"""boxd power source and battery shutdown, with a faked pinctrl, clock and poweroff.

Run:  python3 tests/test_power.py
"""
import importlib.util, json, os, sys, tempfile, threading, unittest, urllib.request
from http.server import ThreadingHTTPServer

TMP = tempfile.mkdtemp()
os.environ["BOXD_STATE"] = TMP
spec = importlib.util.spec_from_file_location("boxd", os.path.join(os.path.dirname(__file__), "..", "files", "boxd", "boxd.py"))
boxd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boxd)

LO = " 3: ip    pu | lo // GPIO3 = input\n"
HI = " 3: ip    pu | hi // GPIO3 = input\n"
I2C = " 3: a0    pu | hi // GPIO3 = SCL1\n"


class Box:
    """A PowerWatch with a fake pin, clock and poweroff. Each tick moves the clock 2 s."""

    def __init__(self, conf="PISUGAR=yes\nBATTERY_SHUTDOWN_MIN=20\n", state=None, boot_time=0):
        self.dir = tempfile.mkdtemp()
        self.conf = os.path.join(self.dir, "power.conf")
        self.set_conf(conf)
        self.pin, self.t, self.offs = LO, 1000.0, 0
        self.state = state or os.path.join(self.dir, "power-source.json")
        self.w = boxd.PowerWatch(run_cmd=lambda cmd: self.pin, now=lambda: self.t, poweroff=self.off,
                                 conf=self.conf, state=self.state, boot_time=boot_time)

    def set_conf(self, text):
        with open(self.conf, "w") as f:
            f.write(text)

    def off(self):
        self.offs += 1

    def ticks(self, n, pin=None):
        if pin is not None:
            self.pin = pin
        for _ in range(n):
            self.w.tick(); self.t += 2
        return self.w.source


class PinTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(boxd.pin_source(LO), "external")
        self.assertEqual(boxd.pin_source(HI), "battery")
        self.assertEqual(boxd.pin_source(" 3: ip | lo // GPIO3 = input"), "external")   # no pull field
        self.assertIsNone(boxd.pin_source(I2C))                    # pin not an input
        self.assertIsNone(boxd.pin_source(""))                     # missing command
        self.assertIsNone(boxd.pin_source("pinctrl: error"))       # unparsable
        self.assertIsNone(boxd.pin_source(" 2: ip pu | lo"))       # another pin

    def test_lo_and_hi(self):
        b = Box()
        self.assertEqual(b.ticks(3, LO), "external")
        self.assertEqual(b.ticks(3, HI), "battery")

    def test_blip_changes_nothing(self):
        b = Box()
        b.ticks(3, LO)
        b.ticks(1, HI)
        self.assertEqual(b.ticks(3, LO), "external")
        self.assertEqual(b.w.status()["on_battery_seconds"], None)

    def test_flapping_stays(self):
        b = Box()
        b.ticks(3, LO)
        for _ in range(10):
            b.ticks(2, HI); b.ticks(2, LO)
        self.assertEqual(b.w.source, "external")

    def test_missing_unparsable_not_input(self):
        for out in ("", "garbage", I2C):
            b = Box()
            b.ticks(3, LO)
            self.assertEqual(b.ticks(3, out), "unknown", out)

    def test_never_guesses_external(self):
        b = Box()
        self.assertEqual(b.ticks(5, ""), "unknown")

    def test_off_without_pisugar(self):
        b = Box(conf="PISUGAR=no\n")
        self.assertEqual(b.ticks(5, HI), "unknown")   # a bare Pi reads hi: must not count as battery
        self.assertEqual(b.offs, 0)


class ShutdownTests(unittest.TestCase):
    def test_fires_at_n_minutes(self):
        b = Box(conf="PISUGAR=yes\nBATTERY_SHUTDOWN_MIN=20\n")
        b.ticks(3, LO)
        b.ticks(3, HI)                  # battery since the first hi read
        b.ticks(20 * 30 - 3)            # last read 2 s before the 20 minutes are up
        self.assertEqual(b.offs, 0)
        self.assertEqual(b.w.status()["shutdown_in_seconds"], 0)
        b.ticks(1)                      # the read at exactly 20 min
        self.assertEqual(b.offs, 1)
        b.ticks(30)                     # only once
        self.assertEqual(b.offs, 1)

    def test_cancels_on_wall_power(self):
        b = Box(conf="PISUGAR=yes\nBATTERY_SHUTDOWN_MIN=1\n")
        b.ticks(3, LO); b.ticks(20, HI)
        self.assertEqual(b.ticks(3, LO), "external")
        self.assertIsNone(b.w.status()["shutdown_in_seconds"])
        b.ticks(100)
        self.assertEqual(b.offs, 0)
        b.ticks(3, HI)                  # a new battery period starts its own full countdown
        self.assertEqual(b.w.status()["shutdown_in_seconds"], 60 - 6)   # 3 reads, 2 s apart

    def test_disabled_with_zero(self):
        b = Box(conf="PISUGAR=yes\nBATTERY_SHUTDOWN_MIN=0\n")
        b.ticks(3, HI); b.ticks(2000)
        self.assertEqual(b.offs, 0)
        self.assertIsNone(b.w.status()["shutdown_in_seconds"])

    def test_bad_config_uses_default(self):
        for bad in ("abc", "-5", "1.5", "99999", ""):
            self.assertEqual(boxd.power_conf(f"PISUGAR=yes\nBATTERY_SHUTDOWN_MIN={bad}\n"), (True, boxd.SHUTDOWN_MIN_DEFAULT), bad)
        self.assertEqual(boxd.power_conf('BATTERY_SHUTDOWN_MIN="5"  # minutes\nPISUGAR=yes'), (True, 5))
        self.assertEqual(boxd.power_conf(""), (False, boxd.SHUTDOWN_MIN_DEFAULT))

    def test_config_change_needs_no_restart(self):
        b = Box(conf="PISUGAR=yes\nBATTERY_SHUTDOWN_MIN=20\n")
        b.ticks(3, HI); b.ticks(60)     # 2 min on battery
        b.set_conf("PISUGAR=yes\nBATTERY_SHUTDOWN_MIN=1\n")
        b.ticks(1)
        self.assertEqual(b.offs, 1)

    def test_resume_after_boxd_restart_not_after_reboot(self):
        b = Box(); b.ticks(3, HI); b.ticks(30)
        since = b.w.since
        again = Box(state=b.state, boot_time=0)            # boxd restarted, same boot
        again.t = b.t; again.ticks(3, HI)
        self.assertEqual(again.w.since, since)
        rebooted = Box(state=b.state, boot_time=b.t - 5)   # the box booted after that battery start
        rebooted.t = b.t; rebooted.ticks(3, HI)
        self.assertNotEqual(rebooted.w.since, since)


class ApiTests(unittest.TestCase):
    def test_shape(self):
        keys = {"source", "on_battery_seconds", "shutdown_in_seconds", "shutdown_after_min"}
        b = Box(); b.ticks(3, LO)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), boxd.H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            for pin, want in ((LO, "external"), (HI, "battery"), ("", "unknown")):
                b.ticks(3, pin)
                boxd.power = b.w
                with urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/power") as r:
                    d = json.load(r)
                self.assertTrue(keys <= set(d), d)
                self.assertEqual(d["source"], want)
                self.assertEqual(d["on_battery_seconds"] is None, want != "battery")
                self.assertEqual(d["shutdown_in_seconds"] is None, want != "battery")
                print(f"  /api/power {want}: {d}")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
