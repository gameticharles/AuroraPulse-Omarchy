"""The panel really loads in the shell.

Static checks catch most QML mistakes, but only the shell's own engine knows
whether every file compiles together - a missing import, a type that does not
exist, a property set twice. This syncs the tree into the live plugin
directory, restarts the shell, and reads the shell's log for anything the
engine said about this plugin.

It changes the running desktop, so it only runs when asked:

    AP_LIVE_LOAD=1 python3 tests/test_panel_load.py
"""

import glob
import os
import re
import shutil
import subprocess
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = "aurora-pulse"


def newest_log():
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    logs = glob.glob(os.path.join(runtime, "quickshell", "by-id", "*", "log.log"))
    return max(logs, key=os.path.getmtime) if logs else ""


@unittest.skipUnless(os.environ.get("AP_LIVE_LOAD") == "1",
                     "set AP_LIVE_LOAD=1 to restart the shell and check the plugin loads")
class PanelLoads(unittest.TestCase):
    def test_the_shell_loads_the_plugin_without_complaint(self):
        if not shutil.which("omarchy-restart-shell"):
            self.skipTest("not an Omarchy desktop")
        subprocess.run([os.path.join(ROOT, "tools", "dev-sync.sh"), "--no-reload"],
                       check=True, capture_output=True, timeout=60)
        started = time.time()
        subprocess.run(["omarchy-restart-shell"], capture_output=True, timeout=60)
        # Wait for a new log, then for the shell to settle.
        log = ""
        for _ in range(60):
            log = newest_log()
            if log and os.path.getmtime(log) > started:
                break
            time.sleep(0.5)
        self.assertTrue(log, "the shell did not start a new log")
        time.sleep(6)
        with open(log, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        failures = re.findall(r".*Plugin widget %s failed.*(?:\n(?!\d{4}-).*)*" % PLUGIN, text)
        self.assertEqual(failures, [], "the shell refused the plugin")
        warnings = [line for line in text.splitlines()
                    if "/plugins/%s/" % PLUGIN in line and "WARN" in line]
        self.assertEqual(warnings, [], "the shell warned about the plugin's QML")
        daemons = subprocess.run(["pgrep", "-f", "%s/ap-ctl daemon" % PLUGIN],
                                 capture_output=True, text=True).stdout.split()
        self.assertTrue(daemons, "no daemon came up with the panel")


if __name__ == "__main__":
    unittest.main()
