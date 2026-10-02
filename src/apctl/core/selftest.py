"""The self test.

`ap-ctl selftest` answers one question: can this machine actually run the
plugin? It checks the things that fail confusingly later - a missing binary,
a bubblewrap that cannot nest, a kernel without the right seccomp support -
and says so plainly instead of leaving the user with a silent bar widget.

It is also the test suite's entry point, so the same assertions that run in CI
are the ones a user runs when something is wrong.
"""

import os
import shutil
import subprocess
import sys
import tempfile

from ..util import netguard, sandbox, textutil



class Report:
    def __init__(self):
        self.rows = []
        self.failures = 0

    def add(self, name, ok, detail="", required=True):
        self.rows.append((name, bool(ok), detail, required))
        if not ok and required:
            self.failures += 1
        return ok

    def render(self):
        width = max(len(name) for name, _o, _d, _r in self.rows)
        lines = []
        for name, ok, detail, required in self.rows:
            mark = "ok  " if ok else ("FAIL" if required else "warn")
            lines.append("  %s  %-*s  %s" % (mark, width, name, detail))
        return "\n".join(lines)


def _version(binary, *args):
    try:
        out = subprocess.run([binary, *args], capture_output=True, timeout=8,
                             text=True)
        return (out.stdout or out.stderr).strip().splitlines()[0][:60]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "?"


def _check_inside(report):
    """Re-enter a sandbox and ask the process inside whether it is sealed.

    The script is handed over as an inherited file descriptor rather than a
    path, because the sandbox has no /home and no way to be given one.
    """
    try:
        cmd, fds = sandbox.sandbox_command("audio", with_script=True)
    except sandbox.SandboxUnavailable as exc:
        return False, str(exc)[:80]
    try:
        proc = subprocess.run(
            cmd + ["--", sandbox.SANDBOX_SCRIPT, "selftest", "--inside"],
            capture_output=True, pass_fds=fds, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:80]

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        return True, out or "denied calls return the expected errno"
    return False, (out or err or "exit %d" % proc.returncode)[:90]


def run_inside():
    """`ap-ctl selftest --inside`: only the isolation assertions.

    Prints one word, so the parent can report exactly what failed.
    """
    if sandbox.sandbox_is_sealed():
        return 0
    print("NOT-SEALED", flush=True)
    return 1


def run():
    report = Report()

    report.add("python", sys.version_info[:2] >= (3, 9),
               "%d.%d" % sys.version_info[:2])
    report.add("kernel seccomp", os.path.exists("/proc/sys/kernel/seccomp/actions_avail"),
               "actions available")

    # The same checks the Settings › Health page shows.
    from . import doctor
    for check in doctor.binaries() + [doctor.ytdlp(), doctor.python_modules()] \
            + doctor.folders():
        if not check:
            continue
        detail = check["detail"] + (("  -> " + check["fix"]) if check["fix"] else "")
        report.add(check["name"], check["ok"], detail, required=check["required"])

    # The sandbox has to work *here*, not just in principle. Both profiles
    # are exercised, because the video profile is the one with the exotic
    # bind mounts.
    for profile in ("audio", "video"):
        try:
            cmd, fds = sandbox.sandbox_command(profile)
        except sandbox.SandboxUnavailable as exc:
            report.add("bubblewrap %s" % profile, False, str(exc))
            continue
        with tempfile.TemporaryDirectory(prefix="ap-selftest-") as tmp:
            probe = (
                "import os,socket,ctypes,sys\n"
                "print('isolation-ok')\n"
                "sys.exit(0 if not os.path.exists('/home') else 9)\n"
            )
            try:
                proc = subprocess.run(
                    cmd + ["--", "/usr/bin/python3", "-c", probe],
                    capture_output=True, pass_fds=fds, timeout=30)
                ok = proc.returncode == 0 and b"isolation-ok" in proc.stdout
                detail = "seccomp + namespaces" if ok else \
                    (proc.stderr.decode("utf-8", "replace").strip()[:80] or
                     "exit %d" % proc.returncode)
            except (OSError, subprocess.SubprocessError) as exc:
                ok, detail = False, str(exc)[:80]
            report.add("sandbox %s" % profile, ok, detail)

    # The isolation check has to run *inside* a sandbox. On the host it would
    # be expected to fail, because the host has capabilities and a /home, so
    # running it there would prove nothing at all.
    sealed, sealed_detail = _check_inside(report)
    report.add("isolation self-check", sealed, sealed_detail)

    report.add("offline guard", netguard.offline() is False, "network policy on")
    for host, port, want in (("127.0.0.1", 443, False), ("10.0.0.1", 443, False),
                             ("example.com", 22, False), ("example.com", 443, True)):
        try:
            netguard.public_connection(host, port, (22, 80, 443), seconds=6)
            got = True
        except netguard.Blocked:
            got = False
        except OSError:
            got = False
        report.add("net %s:%d" % (host, port), got == want,
                   "reachable" if got else "refused", required=False)

    text = textutil.text(" <b>x</b>\u202e  ", 40)
    report.add("text sanitiser", "<" not in text and "\u202e" not in text,
               "markup and overrides removed", required=False)

    print("AuroraPulse self test")
    print(report.render())
    if report.failures:
        print("\n%d required check(s) failed." % report.failures)
    else:
        print("\nAll required checks passed.")
    return 1 if report.failures else 0
