#!/usr/bin/env python3
"""claude-code-voice — resident Kokoro TTS daemon.

Three design decisions worth knowing, because each fixes a problem that is not
obvious until you hit it:

1. WHY A DAEMON.  Loading the ONNX model costs ~0.4s, but the *first* synthesis
   costs ~5s (graph warmup + espeak init); only later calls hit the real ~1s
   cost. A one-shot process pays that 5s on every single turn. A resident one
   pays it once at startup, into silence, and every turn after is warm.

2. WHY STREAMING.  Synthesizing a whole reply before playing anything makes
   time-to-first-sound scale with reply length (4s+ on a long answer, and worse
   the more Claude says). We split into sentence chunks and start playing chunk
   one while the rest synthesize behind it — first sound in ~1s, near-constant
   regardless of length.

3. WHY SESSION-AWARE.  Multiple Claude Code sessions (cmux tabs, worktrees,
   parallel agents) fire Stop hooks independently. A single global "interrupt on
   new turn" rule lets whichever session finished last cut off whatever another
   was mid-sentence saying, with no clue whose voice you were even hearing. So:
       same session   -> barge in   (you want its latest answer, not a backlog)
       other session  -> queue      (announced by project name, never clobbered)

Protocol — newline-delimited UTF-8 over a unix socket:
    {"session","label","text"}\\n  enqueue; reply "ok\\n"
    <bare text>\\n                 same, as an anonymous session
    __STOP__\\n                    drop everything, silence; reply "ok\\n"
    __PING__\\n                    reply "pong\\n"
    __QUIT__\\n                    shut down
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import warnings

warnings.filterwarnings("ignore")

VOICE_HOME = os.environ.get(
    "CLAUDE_VOICE_HOME", os.path.join(os.path.expanduser("~"), ".claude", "voice")
)


def socket_path(voice_home: str) -> str:
    """A short, per-user, per-home socket path.

    AF_UNIX paths are capped near 104 bytes on macOS, and putting the socket
    inside VOICE_HOME blows that limit for anyone with a deep home directory --
    bind() then dies with "AF_UNIX path too long" and the daemon never starts.
    So the socket lives in a short 0700 dir under /tmp, named by a hash of
    VOICE_HOME so separate homes never collide. The bash side computes the
    identical path; keep the two in sync.
    """
    digest = hashlib.md5(voice_home.encode()).hexdigest()[:8]
    d = f"/tmp/ccvoice-{os.getuid()}"
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return os.path.join(d, f"{digest}.sock")


SOCK = socket_path(VOICE_HOME)
MODELS = os.path.join(VOICE_HOME, "models")
MODEL = os.path.join(MODELS, "kokoro-v1.0.onnx")
VOICES = os.path.join(MODELS, "voices-v1.0.bin")
VOICE_FILE = os.path.join(VOICE_HOME, "voice")
SPEED_FILE = os.path.join(VOICE_HOME, "speed")
ANNOUNCE_FILE = os.path.join(VOICE_HOME, "announce")   # always | smart | off
VOICEMODE_FILE = os.path.join(VOICE_HOME, "voicemode")  # fixed | project
VOICEMAP_FILE = os.path.join(VOICE_HOME, "voicemap")    # {label: voice}
LOG = os.path.join(VOICE_HOME, "daemon.log")

IDLE_TIMEOUT = 30 * 60  # exit after this long with no work; restarts on demand

# Kokoro's prosody goes flat and clipped on very short fragments, so don't let
# "OK." become its own utterance: merge up to MIN, hard-split beyond MAX.
MIN_CHUNK = 100
MAX_CHUNK = 300

# Re-announce a session's name if it's been quiet this long, so you're never
# left guessing which project just started talking after a lull.
RELABEL_AFTER = 90.0

DEFAULT_VOICE = "af_heart"

# Pool for per-project voices, ordered so adjacent picks differ in BOTH gender
# and accent -- the whole point is telling two terminals apart by ear, and
# similar-sounding neighbours would defeat that. All are top-graded Kokoro voices.
VOICE_POOL = [
    "af_heart",     # US female (Kokoro's reference voice)
    "bm_george",    # UK male
    "am_michael",   # US male
    "bf_emma",      # UK female
    "af_bella",     # US female, warmer
    "bm_lewis",     # UK male, deeper
    "am_fenrir",    # US male, brighter
    "bf_isabella",  # UK female, lower
]

# Voice assignment is a read-modify-write on a shared file; serialize it.
_voicemap_lock = threading.Lock()


def log(msg: str) -> None:
    try:
        with open(LOG, "a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def read_setting(path: str, default: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip() or default
    except OSError:
        return default


def find_player() -> list[str] | None:
    """The command that plays a wav file, per platform."""
    for cmd in (["afplay"], ["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


PLAYER = find_player()


def pick_voice(label: str, session: str) -> str:
    """Which voice reads this reply.

    In 'project' mode every context gets its own voice, so you can follow two
    terminals by ear without waiting to hear the project name.

    Keyed on the LABEL (project + branch), never the session id: session ids are
    regenerated when you restart a chat, which would reshuffle every voice daily.
    A label is stable -- `api-server` sounds the same tomorrow -- and two
    worktrees of one repo still get different voices, which is exactly the case
    that's hardest to tell apart by ear.
    """
    default = read_setting(VOICE_FILE, DEFAULT_VOICE)
    if read_setting(VOICEMODE_FILE, "fixed") != "project":
        return default

    key = (label or session or "").strip()
    if not key:
        return default

    with _voicemap_lock:
        try:
            with open(VOICEMAP_FILE) as fh:
                mapping = json.load(fh)
            if not isinstance(mapping, dict):
                mapping = {}
        except (OSError, json.JSONDecodeError):
            mapping = {}

        if key in mapping:  # already assigned, or explicitly pinned
            return mapping[key]

        # Hashing the label collides badly at small N -- 10 projects over 8
        # voices put three of them on the same voice, which defeats the point.
        # Assign the least-used voice instead, then persist: distinct voices
        # until the pool is exhausted, and stable forever after.
        used = list(mapping.values())
        voice = min(VOICE_POOL, key=lambda v: (used.count(v), VOICE_POOL.index(v)))
        mapping[key] = voice
        try:
            tmp = f"{VOICEMAP_FILE}.tmp"
            with open(tmp, "w") as fh:
                json.dump(mapping, fh, indent=2, sort_keys=True)
            os.replace(tmp, VOICEMAP_FILE)
        except OSError as exc:
            log(f"could not persist the voice map: {exc}")
        return voice


def is_speakable_label(label: str) -> bool:
    """Is this label worth reading aloud, or is it an opaque id?

    Hearing "seven b, three f, nine a, one c" before every reply is worse than
    hearing nothing at all. Only announce names a human would recognise.
    """
    if not label:
        return False
    core = label.split(",")[0].strip()
    if len(core) < 2:
        return False
    letters = sum(c.isalpha() for c in core)
    digits = sum(c.isdigit() for c in core)
    if letters < 2 or digits > letters:
        return False  # "7b3f9a1c", "2024-11-03"
    bare = re.sub(r"[-_\s]", "", core)
    if len(bare) >= 8 and all(c in "0123456789abcdefABCDEF" for c in bare):
        return False  # uuid-ish / hash-ish
    return True


def chunk_text(text: str) -> list[str]:
    """Split into sentence-ish chunks so playback can start on chunk one."""
    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > MAX_CHUNK:  # long sentence, no break: split on commas
            cut = p.rfind(", ", 0, MAX_CHUNK)
            if cut < MIN_CHUNK:
                cut = MAX_CHUNK
            head, p = p[:cut].strip(), p[cut:].lstrip(", ").strip()
            if head:
                chunks.append(head)
        if not buf:
            buf = p
        elif len(buf) < MIN_CHUNK:
            buf = f"{buf} {p}"
        else:
            chunks.append(buf)
            buf = p
        if len(buf) >= MAX_CHUNK:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks and len(buf) < 40:
            chunks[-1] = f"{chunks[-1]} {buf}"  # no stubby trailing fragment
        else:
            chunks.append(buf)
    return chunks


class Speaker:
    """Serializes utterances across sessions; barges in within a session."""

    def __init__(self, kokoro):
        self.kokoro = kokoro
        self.lock = threading.Condition()
        self.pending: dict[str, dict] = {}  # session -> its latest utterance
        self.order: list[str] = []  # FIFO across sessions
        self.cancel = threading.Event()  # cancels the utterance in flight
        self.speaking: str | None = None
        self.player: subprocess.Popen | None = None
        self.last_session: str | None = None
        self.last_spoke_at = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    # --- producer -----------------------------------------------------------
    def submit(self, session: str, label: str, text: str) -> None:
        with self.lock:
            # A newer turn from the same session supersedes its queued one:
            # you want that session's latest answer, never a stale backlog.
            superseded = session in self.pending
            self.pending[session] = {"label": label, "text": text}
            if not superseded:
                self.order.append(session)
            # ...and if that session is talking right now, cut it off.
            if self.speaking == session:
                self.cancel.set()
                self._kill_player()
            self.lock.notify()

    def stop_all(self) -> None:
        with self.lock:
            self.pending.clear()
            self.order.clear()
            self.cancel.set()
            self._kill_player()

    def _kill_player(self) -> None:
        if self.player and self.player.poll() is None:
            self.player.terminate()
            try:
                self.player.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.player.kill()
        self.player = None

    # --- consumer -----------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self.lock:
                while not self.order:
                    self.lock.wait()
                session = self.order.pop(0)
                item = self.pending.pop(session, None)
                if item is None:
                    continue
                self.cancel.clear()
                self.speaking = session
            try:
                self._speak(session, item["label"], item["text"])
            except Exception as exc:
                log(f"speak failed: {exc}")
            finally:
                with self.lock:
                    self.speaking = None
                    self.last_session = session
                    self.last_spoke_at = time.time()

    def _speak(self, session: str, label: str, text: str) -> None:
        import soundfile as sf

        voice = pick_voice(label, session)
        try:
            speed = float(read_setting(SPEED_FILE, "1.0"))
        except ValueError:
            speed = 1.0

        # Announce the project only when it's genuinely ambiguous who is
        # talking. In the common single-session case you never hear a prefix.
        with self.lock:
            switched = self.last_session is not None and self.last_session != session
            stale = time.time() - self.last_spoke_at > RELABEL_AFTER
            others_waiting = bool(self.order)
        # off    never name the project
        # smart  only when it's ambiguous who's talking (default)
        # always name it on every reply
        mode = read_setting(ANNOUNCE_FILE, "smart")
        if mode == "on":  # legacy value
            mode = "smart"
        if mode != "off" and is_speakable_label(label):
            if mode == "always" or switched or (stale and others_waiting):
                text = f"{label}. {text}"

        log(f"speak session={session[:8]} label={label!r} voice={voice} chars={len(text)}")

        wavs: queue.Queue = queue.Queue()
        done = threading.Event()

        def player() -> None:
            while True:
                wav = wavs.get()
                if wav is None or self.cancel.is_set():
                    if wav:
                        _unlink(wav)
                    break
                with self.lock:
                    self.player = subprocess.Popen(
                        [*PLAYER, wav],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    proc = self.player
                proc.wait()
                _unlink(wav)
            while not wavs.empty():  # drain a superseded turn
                leftover = wavs.get_nowait()
                if leftover:
                    _unlink(leftover)
            done.set()

        threading.Thread(target=player, daemon=True).start()

        for chunk in chunk_text(text):
            if self.cancel.is_set():
                break
            try:
                samples, rate = self.kokoro.create(
                    chunk, voice=voice, speed=speed, lang="en-us"
                )
            except Exception as exc:
                log(f"synth failed (voice={voice!r}): {exc}; retrying {DEFAULT_VOICE}")
                try:
                    samples, rate = self.kokoro.create(
                        chunk, voice=DEFAULT_VOICE, speed=speed, lang="en-us"
                    )
                except Exception as exc2:
                    log(f"chunk dropped: {exc2}")
                    continue
            fd, wav = tempfile.mkstemp(suffix=".wav", prefix="ccvoice-")
            os.close(fd)
            sf.write(wav, samples, rate)
            wavs.put(wav)

        wavs.put(None)
        done.wait()


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def main() -> int:
    os.makedirs(VOICE_HOME, exist_ok=True)

    if PLAYER is None:
        log("no audio player found (need afplay, paplay, aplay or ffplay)")
        return 3
    if not (os.path.exists(MODEL) and os.path.exists(VOICES)):
        log(f"model files missing under {MODELS} — run: claude-voice install-kokoro")
        return 2

    if os.path.exists(SOCK):
        os.unlink(SOCK)

    # Sweep wavs orphaned by a hard kill (SIGKILL skips our own cleanup).
    for stale in glob.glob(os.path.join(tempfile.gettempdir(), "ccvoice-*.wav")):
        _unlink(stale)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(16)
    srv.settimeout(60)

    from kokoro_onnx import Kokoro

    kokoro = Kokoro(MODEL, VOICES)

    # Burn the expensive first synthesis into silence now, so the user's first
    # real turn is warm (~1s) rather than cold (~5s).
    t0 = time.time()
    try:
        kokoro.create("Ready.", voice=DEFAULT_VOICE, speed=1.0, lang="en-us")
    except Exception as exc:
        log(f"warmup failed: {exc}")

    speaker = Speaker(kokoro)
    log(f"daemon up; warmup {time.time() - t0:.2f}s; player={PLAYER[0]}; socket {SOCK}")

    def shutdown(*_):
        speaker.stop_all()
        _unlink(SOCK)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    last_seen = time.time()
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            if time.time() - last_seen > IDLE_TIMEOUT:
                log("idle timeout; exiting")
                shutdown()
            continue

        last_seen = time.time()
        with conn:
            try:
                data = conn.recv(1 << 20).decode("utf-8", "replace").strip()
            except OSError:
                continue
            if not data:
                continue

            if data == "__PING__":
                conn.sendall(b"pong\n")
                continue
            if data == "__STOP__":
                speaker.stop_all()
                conn.sendall(b"ok\n")
                continue
            if data == "__QUIT__":
                conn.sendall(b"ok\n")
                shutdown()

            session, label, text = "anon", "", data
            if data.startswith("{"):
                try:
                    msg = json.loads(data)
                    session = msg.get("session") or "anon"
                    label = msg.get("label") or ""
                    text = (msg.get("text") or "").strip()
                except (json.JSONDecodeError, AttributeError):
                    pass
            if not text:
                continue

            # Reply first: the hook must never block Claude's next turn.
            try:
                conn.sendall(b"ok\n")
            except OSError:
                pass
            speaker.submit(session, label, text)


if __name__ == "__main__":
    sys.exit(main())
