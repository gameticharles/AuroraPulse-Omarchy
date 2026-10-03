"""Downloads: YouTube, YouTube Music and podcast episodes, kept for offline.

One job at a time, in order, on its own thread, each a yt-dlp process with
its progress read line by line. Audio goes to ~/Music/AuroraPulse, video to
~/Videos/AuroraPulse - folders the local library scans - so a finished
download shows up in the Local tab and plays with no network at all.

A job can be cancelled while queued or running; a cancelled or failed job
leaves no partial file behind. The list is kept in the library database, so a
download interrupted by a reboot or a shell restart starts again on its own.
"""

import os
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid

from ..util import textutil
from . import resolver
from ..util import sandbox

AUDIO_FORMATS = ("original", "mp3", "m4a", "opus", "flac", "wav")
VIDEO_FORMATS = ("original", "mp4", "mkv", "webm")
VIDEO_HEIGHTS = {"480p": 480, "720p": 720, "1080p": 1080, "1440p": 1440,
                 "2160p": 2160, "best": 0}
# Cover art goes into the file where ffmpeg can put it. The others need
# mutagen, which is not a dependency here, so the cover is saved next to the
# file instead - the library uses a picture named like the track.
EMBEDS_ART = ("mp3", "m4a", "mp4", "mkv")
MAX_JOBS_KEPT = 50
_PROGRESS = re.compile(r"^AP\s+(\S+)\s+(\S+)\s+(\S+)")


def _user_dir(kind, fallback):
    try:
        out = subprocess.run(["xdg-user-dir", kind], capture_output=True, text=True,
                             timeout=3).stdout.strip()
        if out and os.path.isdir(out) and out != os.path.expanduser("~"):
            return out
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return os.path.expanduser(fallback)


def audio_dir():
    return os.path.join(_user_dir("MUSIC", "~/Music"), "AuroraPulse")


def video_dir():
    return os.path.join(_user_dir("VIDEOS", "~/Videos"), "AuroraPulse")


def recordings_dir():
    return os.path.join(audio_dir(), "Recordings")


def staged_file(staging, path):
    """`path` if it names a regular file directly inside `staging`, else "".

    What a sandboxed tool reports is a request, not a fact. yt-dlp prints the
    file it wrote, and moving that path unchecked would let a compromised
    downloader have any file of the user's moved out of place. Only a plain
    file - not a symlink, directory or device, checked without following
    links - sitting in this job's own folder counts.
    """
    if not path or not staging or os.path.islink(staging):
        return ""
    name = os.path.basename(path)
    candidate = os.path.join(staging, name)
    if name in ("", ".", "..") or \
            os.path.normpath(path) != os.path.normpath(candidate):
        return ""
    try:
        info = os.lstat(candidate)
    except OSError:
        return ""
    return candidate if stat.S_ISREG(info.st_mode) else ""


def make_readable(path):
    """chmod 644 a file we just moved into place, through a descriptor opened
    without following links: a symlink swapped in by a sandboxed writer is
    refused (OSError) instead of having its target's permissions changed."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file: %s" % path)
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)


def _safe_name(value, limit=120):
    text = textutil.text(value, limit * 2)
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit] or "AuroraPulse"


class Downloads:
    def __init__(self, on_change=None, on_done=None, log=None, on_failed=None,
                 library=None):
        self.on_change = on_change or (lambda jobs: None)
        self.on_done = on_done or (lambda job: None)
        self.on_failed = on_failed or (lambda job: None)
        self.log = log or (lambda message: None)
        self.library = library
        self._jobs = self._restore()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._proc = None
        self._running_id = None
        self._last_emit = 0.0
        threading.Thread(target=self._run, name="ap-downloads", daemon=True).start()

    def _restore(self):
        """Jobs from last time. One that was running when the daemon went
        away starts again from nothing: its staging folder is gone or stale."""
        if self.library is None:
            return []
        try:
            jobs = self.library.load_downloads()
        except Exception as exc:  # noqa: BLE001
            self.log("download list not restored: %s" % exc)
            return []
        for job in jobs:
            if job.get("state") == "running":
                job.update(state="queued", progress=0.0, speed="", eta="")
        return jobs[:MAX_JOBS_KEPT]

    def pending(self):
        with self._lock:
            return sum(1 for j in self._jobs if j["state"] in ("queued", "running"))

    # -- public ------------------------------------------------------------

    def jobs(self):
        with self._lock:
            return [dict(j) for j in self._jobs]

    def add(self, entry, kind="audio", fmt="", quality="1080p", artwork=True):
        """Queue a download of a MediaItem. Returns the job."""
        url = str(entry.get("url") or "")
        if not url.startswith(("http://", "https://")):
            raise ValueError("there is nothing to download for that")
        if entry.get("is_live") or entry.get("source") in ("radio", "tv"):
            raise ValueError("a live stream cannot be downloaded; record it instead")
        kind = "video" if kind == "video" else "audio"
        formats = VIDEO_FORMATS if kind == "video" else AUDIO_FORMATS
        fmt = fmt if fmt in formats else ("mp4" if kind == "video" else "mp3")
        with self._lock:
            for job in self._jobs:
                if job["uid"] == entry.get("uid") and job["kind"] == kind \
                        and job.get("format") == fmt \
                        and job["state"] in ("queued", "running", "done"):
                    return dict(job)
            job = {
                "id": uuid.uuid4().hex[:10], "uid": entry.get("uid", ""),
                "title": textutil.text(entry.get("title"), 200),
                "artist": textutil.text(entry.get("artist"), 200),
                "source": entry.get("source", ""), "url": url, "kind": kind,
                "art": entry.get("art") or {}, "state": "queued",
                "format": fmt, "quality": quality if quality in VIDEO_HEIGHTS else "1080p",
                "artwork": bool(artwork),
                "progress": 0.0, "speed": "", "eta": "", "path": "", "error": "",
                "added": int(time.time()),
            }
            self._jobs.insert(0, job)
            del self._jobs[MAX_JOBS_KEPT:]
        self._changed(force=True)
        self._wake.set()
        return dict(job)

    def cancel(self, job_id):
        with self._lock:
            job = next((j for j in self._jobs if j["id"] == job_id), None)
            if job is None:
                return False
            if job["state"] == "queued":
                job["state"] = "cancelled"
            elif job["state"] == "running" and self._proc is not None:
                job["state"] = "cancelled"
                try:
                    os.killpg(self._proc.pid, signal.SIGTERM)
                except OSError:
                    pass
            elif job["state"] in ("done", "failed", "cancelled"):
                # A finished entry: "cancel" removes it from the list (the
                # file, if any, stays on disk).
                self._jobs.remove(job)
        self._changed(force=True)
        return True

    def shutdown(self):
        proc = self._proc
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass

    # -- worker ------------------------------------------------------------

    def _changed(self, force=False):
        now = time.monotonic()
        if force or now - self._last_emit >= 0.8:
            self._last_emit = now
            jobs = self.jobs()
            self.on_change(jobs)
            if force and self.library is not None:
                try:
                    self.library.save_downloads(jobs)
                except Exception as exc:  # noqa: BLE001
                    self.log("download list not saved: %s" % exc)

    def _next(self):
        with self._lock:
            for job in reversed(self._jobs):       # oldest queued first
                if job["state"] == "queued":
                    job["state"] = "running"
                    return job
        return None

    def _run(self):
        while True:
            job = self._next()
            if job is None:
                self._wake.wait(30)
                self._wake.clear()
                continue
            self._changed(force=True)
            try:
                self._download(job)
            except Exception as exc:  # noqa: BLE001 - a job must not kill the worker
                with self._lock:
                    if job["state"] == "running":
                        job["state"] = "failed"
                        job["error"] = textutil.text(str(exc), 200)
            self._changed(force=True)
            if job["state"] == "done":
                self.on_done(dict(job))
            elif job["state"] == "failed":
                self.on_failed(dict(job))

    def _download(self, job):
        """Run one job in its own staging folder, then move the result in.

        yt-dlp only ever sees an empty private folder. It used to write into
        Music or Videos directly, where a second download of the same track
        could pick up an existing file as its own intermediate and delete it
        after converting - a download must never touch a file it did not
        create. It runs sandboxed: network, and that one folder, nothing else.
        """
        folder = video_dir() if job["kind"] == "video" else audio_dir()
        os.makedirs(folder, exist_ok=True)
        name = _safe_name("%s - %s" % (job["artist"], job["title"]) if job["artist"]
                          and job["artist"] not in job["title"] else job["title"])
        staging = os.path.join(folder, ".aurorapulse-%s" % job["id"])
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        template = os.path.join(staging, name + ".%(ext)s")
        argv = [resolver.YTDLP, "--no-config", "--ignore-config", "--no-playlist",
                "--newline", "--no-mtime", "--no-cache-dir", "--no-part",
                "--socket-timeout", "20", "--retries", "3",
                "--progress-template",
                "download:AP %(progress._percent_str)s %(progress._speed_str)s "
                "%(progress._eta_str)s",
                "--print", "after_move:FILE %(filepath)s",
                "--embed-metadata", "-o", template]
        argv += self._format_args(job)
        argv += ["--", job["url"]]
        try:
            cmd, fds = sandbox.tool_command(argv, profile="fetcher", write=[staging])
        except sandbox.SandboxUnavailable:
            # Never yt-dlp outside the sandbox. The job fails with the reason,
            # which names what to install.
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            self._proc = proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                stdin=subprocess.DEVNULL, pass_fds=fds, start_new_session=True)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        last_error = ""
        path = ""
        for line in proc.stdout:
            line = line.strip()
            match = _PROGRESS.match(line)
            if match:
                try:
                    percent = float(match.group(1).rstrip("%"))
                except ValueError:
                    percent = job["progress"]
                with self._lock:
                    job["progress"] = max(job["progress"], min(100.0, percent))
                    job["speed"] = textutil.text(match.group(2), 20)
                    job["eta"] = textutil.text(match.group(3), 20)
                self._changed()
            elif line.startswith("FILE "):
                path = line[5:].strip()
            elif line.startswith("ERROR"):
                last_error = line
        code = proc.wait()
        proc.stdout.close()
        self._proc = None
        try:
            with self._lock:
                if job["state"] == "cancelled":
                    return
            # yt-dlp has exited and its sandbox with it, so nothing can swap
            # the file between this check and the move.
            result = staged_file(staging, path) if code == 0 else ""
            if result:
                final = self._publish(staging, result, folder)
                with self._lock:
                    job.update(state="done", progress=100.0, path=final, speed="", eta="")
            else:
                if code == 0 and path:
                    error = ("the downloader named a file outside its own folder; "
                             "nothing was moved")
                    self.log("download %s: refused result %r" % (job["id"], path[:200]))
                else:
                    error = last_error or "yt-dlp exited with %d" % code
                with self._lock:
                    job.update(state="failed", error=textutil.text(error, 200))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _publish(staging, path, folder):
        """Move a finished file (and its cover, if saved beside it) out of
        staging under a name nothing else is using. Both must be plain files
        in this job's staging folder; anything else is never touched."""
        source = staged_file(staging, path)
        if not source:
            raise ValueError("not a file in this job's staging folder")
        stem, ext = os.path.splitext(os.path.basename(source))
        target = os.path.join(folder, stem + ext)
        n = 2
        while os.path.exists(target):
            target = os.path.join(folder, "%s (%d)%s" % (stem, n, ext))
            n += 1
        shutil.move(source, target)
        cover = staged_file(staging, os.path.join(staging, stem + ".jpg"))
        if cover:
            new_stem = os.path.splitext(os.path.basename(target))[0]
            try:
                shutil.move(cover, os.path.join(folder, new_stem + ".jpg"))
            except OSError:
                pass
        return target

    @staticmethod
    def _format_args(job):
        """yt-dlp arguments for the chosen container, codec and quality."""
        fmt = job.get("format") or ""
        args = []
        if job["kind"] == "video":
            height = VIDEO_HEIGHTS.get(job.get("quality") or "1080p", 1080)
            cap = "[height<=%d]" % height if height else ""
            if fmt == "mp4":
                # H.264 and AAC, the pair every player and phone opens; the
                # best stream of any codec is the fallback when there is none.
                args += ["-f", "bv*[vcodec^=avc1]%s+ba[ext=m4a]/bv*%s+ba/b%s/b"
                         % (cap, cap, cap), "--merge-output-format", "mp4"]
            elif fmt == "webm":
                args += ["-f", "bv*[ext=webm]%s+ba[ext=webm]/bv*%s+ba/b%s/b"
                         % (cap, cap, cap), "--merge-output-format", "webm"]
            elif fmt == "mkv":
                args += ["-f", "bv*%s+ba/b%s/b" % (cap, cap), "--merge-output-format", "mkv"]
            else:
                args += ["-f", "bv*%s+ba/b%s/b" % (cap, cap)]
        else:
            args += ["-f", "ba/b", "-x"]
            if fmt == "original":
                pass                      # keep the stream as it came
            elif fmt in ("flac", "wav"):
                args += ["--audio-format", fmt]
            else:
                args += ["--audio-format", fmt, "--audio-quality", "0"]
        if job.get("artwork", True):
            container = fmt if fmt not in ("original", "") else ""
            if job["kind"] == "video" and fmt == "original":
                container = "mkv"
                args += ["--merge-output-format", "mkv"]
            if container in EMBEDS_ART:
                args += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
            else:
                args += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
        return args



def available():
    return shutil.which(resolver.YTDLP) is not None or os.path.exists(resolver.YTDLP)
