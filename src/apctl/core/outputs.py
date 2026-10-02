"""Where the sound comes out: this computer's speakers, headphones, HDMI, or a
Bluetooth speaker.

A Bluetooth speaker is not something to cast to - it is an audio output of
this computer, like the built-in speakers. The player's sound leaves through
pw-cat, a PipeWire stream named "AuroraPulse", so choosing an output moves
that one stream; other applications stay where they were. The choice is kept
and handed to every new player (`pw-cat --target`); if that output is gone,
PipeWire falls back to the default one by itself.

Paired Bluetooth audio devices that are not connected are listed too, and
choosing one connects it first.
"""

import json
import re
import shutil
import subprocess
import time

from ..util import textutil

APP_NAME = "AuroraPulse"
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def _run(argv, timeout=5):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc


def _json(argv):
    proc = _run(argv)
    if proc is None or proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def kind_of(properties, name=""):
    bus = str(properties.get("device.bus") or "")
    icon = str(properties.get("device.icon_name") or properties.get("device.form_factor") or "")
    if bus == "bluetooth" or name.startswith("bluez_"):
        return "bluetooth"
    if "hdmi" in name.lower() or "hdmi" in str(properties.get("device.profile.name", "")).lower():
        return "hdmi"
    if "headphone" in icon or "headset" in icon:
        return "headphones"
    return "speaker"


def default_sink():
    proc = _run(["pactl", "get-default-sink"], timeout=3) if shutil.which("pactl") else None
    return (proc.stdout.strip() if proc and proc.returncode == 0 else "")[:200]


def sinks():
    """The outputs PipeWire has now: [{name, description, kind, default}]."""
    if not shutil.which("pactl"):
        return []
    default = default_sink()
    out = []
    for sink in _json(["pactl", "--format=json", "list", "sinks"]):
        name = str(sink.get("name") or "")
        if not name:
            continue
        properties = sink.get("properties") or {}
        out.append({"name": name[:200],
                    "description": textutil.text(sink.get("description") or name, 80),
                    "kind": kind_of(properties, name), "default": name == default})
    return out


def bluetooth_audio(connected_sinks=()):
    """Paired Bluetooth audio devices that are not an output right now."""
    if not shutil.which("bluetoothctl"):
        return []
    proc = _run(["bluetoothctl", "devices", "Paired"])
    if proc is None:
        return []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[0] != "Device" or not _MAC.match(parts[1]):
            continue
        mac = parts[1]
        if any(mac.replace(":", "_") in sink for sink in connected_sinks):
            continue
        info = _run(["bluetoothctl", "info", mac])
        text = info.stdout if info else ""
        icon = re.search(r"Icon:\s*(\S+)", text)
        audio = (icon and icon.group(1).startswith("audio")) or "Audio Sink" in text
        if not audio:
            continue
        out.append({"mac": mac, "description": textutil.text(parts[2], 80),
                    "connected": "Connected: yes" in text})
    return out


def our_streams(runtime=""):
    """PipeWire's index for this daemon's playback stream: tagged with its
    runtime folder, so a second AuroraPulse (a test, another user session)
    is never moved by this one."""
    if not shutil.which("pactl"):
        return []
    mine, untagged = [], []
    for item in _json(["pactl", "--format=json", "list", "sink-inputs"]):
        properties = item.get("properties") or {}
        if properties.get("application.name") != APP_NAME or item.get("index") is None:
            continue
        tag = properties.get("aurorapulse.runtime")
        if not runtime or tag == runtime:
            mine.append(int(item["index"]))
        elif not tag:
            untagged.append(int(item["index"]))
    # A player started by an older version carries no tag.
    return mine or untagged


def move_to(sink, runtime=""):
    """Move the playing stream to `sink` ("" = the default output). Returns
    how many streams moved."""
    target = sink or "@DEFAULT_SINK@"
    moved = 0
    for index in our_streams(runtime):
        proc = _run(["pactl", "move-sink-input", str(index), target])
        if proc is not None and proc.returncode == 0:
            moved += 1
    return moved


def connect_bluetooth(mac, wait=12.0):
    """Connect a paired device and return the name of its output once
    PipeWire has made one, or ""."""
    if not _MAC.match(mac or "") or not shutil.which("bluetoothctl"):
        return ""
    _run(["bluetoothctl", "connect", mac], timeout=20)
    marker = mac.replace(":", "_")
    end = time.monotonic() + wait
    while time.monotonic() < end:
        for sink in sinks():
            if marker in sink["name"]:
                return sink["name"]
        time.sleep(0.5)
    return ""


def listing(chosen=""):
    """Everything the "Play on" menu shows under this computer."""
    now = sinks()
    names = [s["name"] for s in now]
    current = chosen if chosen in names else next((s["name"] for s in now if s["default"]), "")
    return {"outputs": [dict(s, current=s["name"] == current) for s in now],
            "bluetooth": bluetooth_audio(names),
            "chosen": chosen if chosen in names else ""}
