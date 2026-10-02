"""Sandbox construction.

Three profiles, one base. See docs/SECURITY.md for the reasoning; the short
version is that mpv and ffmpeg see a hostile server, and that neither is ever
handed a route out it did not get through the guarded proxy - which is the
property that matters, not the absence of a network namespace.

The daemon script is copied *into* the sandbox as a memfd rather than
bind-mounted, so no host path exists from the inside. The seccomp program is
handed over the same way.
"""

import glob
import os
import shutil
import socket
import struct
import subprocess
import sys

SANDBOX_IPC_FD = 200
# mpv is told to listen here; the relay fanned out to local clients holds the
# other end. Named separately so the player code reads clearly.
IPC_FD = SANDBOX_IPC_FD
SANDBOX_SCRIPT = "/opt/aurora-pulse/ap-ctl"
SANDBOX_PROXY = "/run/aurora-proxy.sock"
# Where the 0700 runtime directory appears inside the sandbox. mpv
# creates its own control socket here, and that socket is the same
# inode the daemon connects to.
SANDBOX_RUNTIME = "/run/aurora"

# Read-only host files the dynamic loader and TLS stack need. Nothing here is
# user data, and every one is optional.
SANDBOX_ETC = (
    "/etc/ld.so.cache",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pkcs11",
    "/etc/gnutls",
    "/etc/localtime",
    "/etc/fonts",
    "/etc/alternatives",
)

# The nodes NVIDIA's userspace driver needs. Resolved at build time because
# the card index depends on the machine.
_NVIDIA_NODES = (["/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm"]
                 + sorted(glob.glob("/dev/nvidia[0-9]*")))

PCM_FORMAT = "s16"
PCM_RATE = 48000
PCM_CHANNELS = 2

PROFILES = {
    "audio": {
        "memory": 1536 << 20,
        "tasks": 256,
        "gpu": False,
        "wayland": False,
        "network": True,
    },
    "video": {
        "memory": 2048 << 20,
        "tasks": 256,
        "gpu": True,
        "wayland": True,
        # Video does get a network namespace, but not a direct one. A TV
        # channel is an HLS stream: segments arrive for as long as you watch,
        # so there is nothing to pre-resolve. The guard is the proxy in
        # core/session.py, which re-checks every address mpv is asked to
        # reach, rather than a missing route - which only stops playback.
        "network": True,
        "proxied": True,
    },
    "artwork": {
        "memory": 512 << 20,
        "tasks": 32,
        "gpu": False,
        "wayland": False,
        "network": False,
    },
    # yt-dlp, for resolving and downloading. It needs the network directly
    # (it speaks to YouTube's own APIs, not a stream a proxy can vet) but
    # nothing of the user's: no home, no other files, only the one folder a
    # download is written to.
    "fetcher": {
        "memory": 2048 << 20,
        "tasks": 128,
        "gpu": False,
        "wayland": False,
        "network": True,
        "share_net": True,
    },
}


class SandboxUnavailable(Exception):
    pass


def _memfd(name, payload):
    fd = os.memfd_create(name, 0)
    os.write(fd, payload)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


# --- seccomp -----------------------------------------------------------------
#
# Classic BPF, assembled here rather than shipped as a binary so the deny list
# is readable and auditable. x86_64 only; elsewhere we return None and the
# sandbox relies on namespaces, mounts and rlimits alone.

# Syscalls the process has no business making inside a media sandbox. Grouped
# so the list reads as intent rather than as a wall of numbers.
_SECCOMP_DENY = frozenset(
    # Process control: nothing here needs to inspect or attach to another task.
    {101,  # ptrace
     155,  # getpgid
     154,  # setpgid
     161,  # chroot
     272,  # unshare
     300,  # setns
     310,  # process_vm_readv
     311,  # process_vm_writev
     312,  # kcmp
     431,  # pidfd_open
     442} | # mount_setattr
    # Kernel modules and kexec: loading code into the kernel.
    {173, 175, 176, 179, 246} |
    # Mounts and namespace changes.
    {163, 165, 166, 167, 168, 303, 425, 426, 427, 428, 429, 430} |
    # Privileged I/O ports and VM-level operations.
    {171, 172, 248} |
    # Kernel attack surface we do not want reachable at all.
    {298,  # perf_event_open
     320,  # bpf
     323} | # userfaultfd
    # Handles that can open a file by inode, bypassing path permissions.
    {304, 313, 321} | # open_by_handle_at, finit_module, execveat
    # Legacy and observability surfaces.
    {103, 212, 249, 250, 433}  # syslog, readahead, ustat, statfs, close_range
)
# Deliberately NOT denied, because bubblewrap's own init needs them before the
# filter is even live, or because a runtime genuinely uses them:
#   sethostname/setdomainname (bwrap --hostname), mount/unshare/setns (bwrap),
#   rt_sigaction (every runtime), socket(AF_VSOCK) (bwrap's parent channel).

_NR_SOCKET = 41
_NR_SOCKETPAIR = 53
_NR_PERSONALITY = 135
_NR_CLONE3 = 435

_ALLOWED_FAMILIES = (socket.AF_UNIX, socket.AF_INET, socket.AF_INET6)

AUDIT_ARCH_X86_64 = 0xC000003E

# BPF instruction classes
_BPF_LD = 0x00
_BPF_JMP = 0x05
_BPF_RET = 0x06
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JA = 0x00
_BPF_JEQ = 0x10
_BPF_K = 0x00

# seccomp return values
_KILL = 0x80000000
_ERRNO = 0x00050000
_ALLOW = 0x7FFF0000
_EPERM = 1
_ENOSYS = 38
_EAFNOSUPPORT = 97


def seccomp_program():
    """A classic-BPF seccomp filter, or None off x86_64."""
    import platform
    if platform.machine() != "x86_64":
        return None

    a = _Asm()
    a.ld(4)                                   # arch
    a.jump_if(_BPF_JEQ, AUDIT_ARCH_X86_64, "arch_ok", None)
    a.ret(_KILL)
    a.label("arch_ok")

    a.ld(0)                                   # syscall number
    # Deny list: a linear chain, one comparison per entry. A miss falls
    # through to the next comparison; a hit jumps straight to the epilogue.
    for nr in sorted(_SECCOMP_DENY):
        a.jump_if(_BPF_JEQ, nr, "deny", None)

    a.jump_if(_BPF_JEQ, _NR_CLONE3, "clone3", None)
    a.jump_if(_BPF_JEQ, _NR_SOCKET, "sock", None)
    a.jump_if(_BPF_JEQ, _NR_SOCKETPAIR, "sock", None)
    a.jump_if(_BPF_JEQ, _NR_PERSONALITY, "personality", None)
    a.ja("allow")

    a.label("sock")                           # args[0] = address family
    a.ld(16)
    for family in _ALLOWED_FAMILIES:
        a.jump_if(_BPF_JEQ, family, "allow", None)
    a.ret(_ERRNO | _EAFNOSUPPORT)

    a.label("personality")                    # args[0] = persona
    a.ld(16)
    a.jump_if(_BPF_JEQ, 0x00000000, "allow", None)
    a.jump_if(_BPF_JEQ, 0xFFFFFFFF, "allow", None)
    a.ret(_ERRNO | _EPERM)

    a.label("clone3")                         # force libc back to clone(2)
    a.ret(_ERRNO | _ENOSYS)

    a.label("deny")
    a.ret(_ERRNO | _EPERM)

    a.label("allow")
    a.ret(_ALLOW)
    return a.assemble()


class _Asm:
    """A minimal, correct classic-BPF assembler with label resolution.

    Every instruction is exactly 8 bytes (code, jt, jf, k), so jump offsets
    can be computed before the targets are known and patched in a second pass.
    Writing this by hand is how sandbox filters end up quietly allowing
    something, so the whole thing is data-driven and unit-tested.

    Note the asymmetry that makes hand-rolling this error-prone: a conditional
    jump puts its offset in the 8-bit jt/jf fields, but an unconditional
    BPF_JA puts its offset in the 32-bit k field.
    """

    _SIZE = 8

    def __init__(self):
        self._ins = []          # (code, k, jt_label, jf_label, ja_label)
        self._labels = {}

    def label(self, name):
        if name in self._labels:
            raise ValueError("duplicate BPF label: %s" % name)
        self._labels[name] = len(self._ins)

    def ld(self, offset):
        self._ins.append((_BPF_LD | _BPF_W | _BPF_ABS, offset, None, None, None))

    def ja(self, label):
        """Unconditional jump. The offset lives in k, not jt."""
        self._ins.append((_BPF_JMP | _BPF_JA, 0, None, None, label))

    def jump_if(self, op, k, jt, jf):
        """Conditional jump. A None jt/jf means fall through to the next one."""
        self._ins.append((_BPF_JMP | op | _BPF_K, k, jt, jf, None))

    def ret(self, k):
        self._ins.append((_BPF_RET | _BPF_K, k, None, None, None))

    def assemble(self):
        offsets, cursor = [], 0
        for _code, _k, _jt, _jf, _ja in self._ins:
            offsets.append(cursor)
            cursor += self._SIZE

        out = bytearray()
        for index, (code, k, jt, jf, ja) in enumerate(self._ins):
            here = offsets[index]

            def relative(name):
                steps = (offsets[self._labels[name]] - here - self._SIZE) // self._SIZE
                if steps < 0:
                    raise ValueError("BPF may not jump backwards (label %s)" % name)
                return steps

            jt_value = relative(jt) if jt is not None else 0
            jf_value = relative(jf) if jf is not None else 0
            if not 0 <= jt_value <= 255 or not 0 <= jf_value <= 255:
                raise ValueError("BPF conditional jump out of range at %d" % index)

            if ja is not None:
                k = relative(ja)          # BPF_JA carries its offset in k
            out += struct.pack("=HBBI", code, jt_value, jf_value, k & 0xFFFFFFFF)
        return bytes(out)



# --- profiles ----------------------------------------------------------------

def find_script():
    """Locate the ap-ctl entry script, whether run from a checkout or installed."""
    here = os.path.abspath(__file__)
    for _ in range(6):
        here = os.path.dirname(here)
        candidate = os.path.join(here, "ap-ctl")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    raise SandboxUnavailable("cannot find the ap-ctl entry script")


def find_package():
    """The directory that contains the apctl package."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(here, "apctl", "__init__.py")):
            return os.path.realpath(here)
        here = os.path.dirname(here)
    raise SandboxUnavailable("cannot find the apctl package")


# The inside-the-sandbox entry point. A zipapp is a zip with a shebang, so a
# single file carries the entry point *and* the whole package: there is no
# second path for the sandbox to be given, and therefore no second thing to
# get wrong.
_ZIPAPP_MAIN = b'''import sys
from apctl.core import daemon
sys.exit(daemon.main(sys.argv[1:]))
'''


def build_zipapp():
    """Build a self-contained executable in memory.

    The sandbox gets a memfd and no host path, so this has to work with no
    filesystem access at all. A zip of the package plus a shebang is the
    smallest thing that satisfies both the interpreter and zipimport.
    """
    import io
    import zipfile

    source_root = find_package()
    package_root = os.path.join(source_root, "apctl")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for directory, dirnames, filenames in os.walk(package_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                full = os.path.join(directory, name)
                relative = os.path.relpath(full, source_root)
                with open(full, "rb") as handle:
                    archive.writestr(relative, handle.read())
        archive.writestr("__main__.py", _ZIPAPP_MAIN)

    return b"#!/usr/bin/python3\n" + buffer.getvalue()


def _script_fd():
    """Copy the self-contained entry point into a memfd.

    No host path exists inside the sandbox, so the executable arrives as a
    file descriptor that is bind-mounted exactly once.
    """
    return _memfd("aurora-ctl", build_zipapp())


def sandbox_command(profile="audio", extra_binds=(), extra_env=(), with_script=False):
    """Build the bwrap argv for a profile. Returns (argv, extra_fds)."""
    if profile not in PROFILES:
        raise ValueError("unknown sandbox profile: %s" % profile)
    settings = PROFILES[profile]

    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxUnavailable(
            "bubblewrap (bwrap) is required; install bubblewrap")

    cmd = [
        bwrap,
        "--unshare-all", "--unshare-user", "--disable-userns",
        "--cap-drop", "ALL", "--die-with-parent", "--new-session",
        "--clearenv", "--hostname", "aurora",
        "--setenv", "PATH", "/usr/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "MALLOC_ARENA_MAX", "2",
        "--ro-bind", "/usr", "/usr",
        # /dev is a private tmpfs, so the only way to touch a real device is
        # to be handed one explicitly. The DRM render node has to be writable:
        # a client opens it O_RDWR to get a GPU context.
        "--dev", "/dev",
        "--proc", "/proc",
        "--size", str(16 << 20), "--tmpfs", "/tmp",
        "--size", str(1 << 20), "--tmpfs", "/run",
        "--dir", "/etc",
    ]

    if settings["gpu"] and os.path.isdir("/dev/dri"):
        cmd += ["--dev-bind", "/dev/dri", "/dev/dri"]

    for link in ("/bin", "/sbin", "/lib", "/lib64"):
        if os.path.islink(link):
            cmd += ["--symlink", os.readlink(link), link]
    for path in SANDBOX_ETC:
        cmd += ["--ro-bind-try", path, path]

    if settings["gpu"]:
        # A DRM client opens the render node O_RDWR to get a GPU context, so
        # this has to be a writable bind. Binding it read-only - which this
        # did, twice, the second time shadowing the first - leaves the node
        # present but unusable, and the player dies with no diagnostic.
        if os.path.isdir("/dev/dri"):
            cmd += ["--dev-bind", "/dev/dri", "/dev/dri"]
        # The proprietary NVIDIA driver does not live on /dev/dri at all. Its
        # EGL stack needs the control and mode-set nodes to talk to the kernel
        # driver, and libdrm needs /sys to enumerate the device. Without both,
        # Mesa cannot create a dri2 screen, so no video output is produced and
        # the only symptom is a stream that silently never starts.
        for node in _NVIDIA_NODES:
            if os.path.exists(node):
                cmd += ["--dev-bind", node, node]
        if os.path.isdir("/dev/nvidia-caps"):
            cmd += ["--ro-bind", "/dev/nvidia-caps", "/dev/nvidia-caps"]
        if os.path.isdir("/sys"):
            cmd += ["--ro-bind", "/sys", "/sys"]

    if settings["wayland"]:
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        socket_path = os.environ.get("WAYLAND_DISPLAY", "")
        if runtime and socket_path:
            full = os.path.join(runtime, socket_path)
            if os.path.exists(full):
                # Also not a read-only bind: connect(2) on a unix socket
                # needs write permission on the socket file, so a ro bind
                # makes the compositor unreachable rather than read-only.
                cmd += ["--bind", full, "/run/wayland-1"]
                cmd += ["--setenv", "WAYLAND_DISPLAY", "wayland-1"]
                cmd += ["--setenv", "XDG_RUNTIME_DIR", "/run"]

    # A video URL is resolved by the daemon before mpv ever sees it, so the
    # video player needs no route out at all. Saying so is the difference
    # between a documented policy and an unenforced one.
    if not settings["network"]:
        cmd += ["--unshare-net"]
    if settings.get("share_net"):
        cmd += ["--share-net"]
        # Name resolution: the real files, wherever their symlinks point.
        for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
                     "/etc/host.conf", "/etc/gai.conf"):
            real = os.path.realpath(name)
            if os.path.exists(real):
                cmd += ["--ro-bind", real, name]

    fds = []
    if with_script:
        fd = _script_fd()
        fds.append(fd)
        cmd += ["--dir", os.path.dirname(SANDBOX_SCRIPT),
                "--perms", "0555", "--file", str(fd), SANDBOX_SCRIPT]

    for mode, source, target in extra_binds:
        cmd += [mode, source, target]
    for name, value in extra_env:
        cmd += ["--setenv", name, value]

    # Deliberately no blanket `--remount-ro /`. Every path that exists comes
    # from a --ro-bind, and the writable ones (/tmp, /run, /dev) are private
    # tmpfs mounts that vanish with the namespace. A remount-ro here would
    # only break mpv's cache and the DRM node while adding no isolation.

    program = seccomp_program()
    if program is not None:
        fd = _memfd("aurora-seccomp", program)
        fds.append(fd)
        cmd += ["--seccomp", str(fd)]

    return cmd, fds


def sandbox_is_sealed():
    """Prove from the inside that the isolation actually took.

    Returning False means the player never starts. Running it unsandboxed
    would be worse than not playing.
    """
    import errno

    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(("CapEff:", "CapPrm:", "CapAmb:", "CapBnd:")):
                    if int(line.split()[1], 16) != 0:
                        return False
    except OSError:
        return False

    # The user's home and session buses must simply not be there.
    for path in ("/home", "/run/systemd", "/run/user", "/var/lib/dbus"):
        if os.path.exists(path):
            return False

    unreachable = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL,
                   errno.EAFNOSUPPORT, errno.EPERM)
    for host, family in (("1.1.1.1", socket.AF_INET),
                         ("2606:4700:4700::1111", socket.AF_INET6)):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect((host, 443))
            return False
        except OSError as exc:
            if exc.errno not in unreachable:
                return False
        finally:
            sock.close()

    # If the seccomp family filter is not live, an AF_VSOCK socket succeeds.
    # This is the check that proves the program was actually applied.
    try:
        probe = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        probe.close()
        return False
    except OSError as exc:
        if exc.errno not in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
            return False
    return True


def rlimits(profile):
    """A preexec_fn that clamps the sandboxed program."""
    import resource
    settings = PROFILES.get(profile, PROFILES["audio"])
    memory = settings["memory"]
    tasks = settings["tasks"]

    def apply():
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_NPROC, (tasks, tasks))
        core = resource.RLIMIT_CORE
        resource.setrlimit(core, (0, 0))
    return apply


def tool_command(argv, profile="artwork", read=(), write=(), env=(), cpu_seconds=None):
    """Wrap one tool invocation (ffprobe, ffmpeg, yt-dlp) in a sandbox.

    Only the paths named here exist inside: `read` read-only, `write`
    writable, each at its own path so the tool's arguments need no rewriting.
    Everything else of the user's is absent. Returns (argv, fds); pass the
    fds to subprocess with pass_fds and close them afterwards.
    """
    binds = []
    for path in read:
        if path and os.path.exists(path):
            binds.append(("--ro-bind", path, path))
    for path in write:
        if path:
            os.makedirs(path, exist_ok=True)
            binds.append(("--bind", path, path))
    cmd, fds = sandbox_command(profile, extra_binds=binds, extra_env=list(env))
    # Limits go on the tool, inside the sandbox. Set on bwrap itself, the
    # process-count limit counts every process the user owns and the clone
    # that creates the namespace fails with EAGAIN.
    settings = PROFILES.get(profile, PROFILES["artwork"])
    limit = []
    if os.path.exists("/usr/bin/prlimit"):
        limit = ["/usr/bin/prlimit", "--as=%d" % settings["memory"], "--core=0"]
        if cpu_seconds:
            limit.append("--cpu=%d" % int(cpu_seconds))
        limit.append("--")
    return cmd + ["--"] + limit + list(argv), fds


def run_tool(argv, profile="artwork", read=(), write=(), env=(), timeout=60,
             stdin=None, text=False):
    """Run a tool sandboxed and wait for it. Returns a CompletedProcess.

    Falls back to running it directly only when bubblewrap itself is missing,
    so a machine without it still works rather than losing artwork and tags.
    """
    import subprocess
    try:
        cmd, fds = tool_command(argv, profile, read, write, env)
    except SandboxUnavailable:
        cmd, fds = list(argv), []
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout,
                              text=text, pass_fds=fds,
                              stdin=None if stdin is not None else subprocess.DEVNULL)
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
