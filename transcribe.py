#!/usr/bin/env python3
"""
Dual-stream transcriber — mics, Bluetooth, and system/meeting audio, labeled separately.
Free, fully local (Whisper on CPU). No API key required.

Captures via PipeWire/PulseAudio (pactl + parec) instead of raw ALSA, so it sees
every real source PipeWire knows about: physical mics, Bluetooth mics, and the
"monitor" of any sink (speakers/headset/Bluetooth) — which is what's actually
playing in a Teams/Zoom/Preply call or anything else on the desktop.
"""
import datetime
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from urllib.parse import quote

import numpy as np
import ttkbootstrap as tb
from faster_whisper import WhisperModel
from tkinterdnd2 import DND_FILES, TkinterDnD
from ttkbootstrap.widgets.scrolled import ScrolledText

IS_WINDOWS = platform.system() == "Windows"
SAMPLE_RATE = 16000
# Audio is cut into chunks at a natural pause between these lengths, so words
# aren't split across chunks; a chunk is forced at MAX if nobody pauses.
MIN_CHUNK_SEC = 4
MAX_CHUNK_SEC = 8
PAUSE_FRAME_SEC = 0.1
PAUSE_RATIO = 0.3      # a frame this quiet relative to the chunk's RMS counts as a pause
MAX_BACKLOG = 3        # chunks queued per stream before the oldest is dropped
SILENCE_RMS = 0.005
# A frozen build lives next to its exe; sessions go there, not into PyInstaller's
# unpack dir (which is a temp folder for --onefile and _internal/ for --onedir).
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
SESSIONS_DIR = APP_DIR / "sessions"
# Models shipped with the app (the Windows build bundles `base`), so they load
# offline with no first-use download. PyInstaller unpacks data under _MEIPASS.
BUNDLED_MODELS_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "models"

MIC_PALETTE = ["#4fc3f7", "#ffca28", "#ba68c8", "#ff8a65", "#aed581", "#f06292", "#90a4ae"]
SPEAKER_PALETTE = ["#00897b", "#26a69a", "#43a047", "#7cb342", "#00acc1", "#5c6bc0", "#8d6e63"]
NOTE_KEY_IGNORE = {"Up", "Down", "Return", "Tab", "Escape"}

MD_PREAMBLE = """\
> **How to read this file (for AI/note-building tools):**
> This is a raw, time-stamped export from a live audio transcriber, meant as
> source material for building a clean notes document — not a finished one.
>
> - **Timeline** entries are chronological. Each is tagged with its source:
>   the *device/speaker name* (e.g. a mic or headset) means it's automatic
>   speech-to-text from that audio stream — expect occasional mishearings,
>   cut-off words, or garbled phrasing, since it's unedited ASR output.
>   Streams can interleave (e.g. two people talking near the same time).
> - **📝 Note** entries were typed by the user in real time during the
>   session to flag something worth keeping — treat these as higher-signal
>   than the surrounding ASR text, and use them to guide what's important.
> - **Attachments** are files the user dropped in during the session,
>   copied into `assets/` next to this file. Each one gets a **📎 Attachment**
>   Timeline entry at the moment it was added, so you can see what was being
>   discussed right then even if no note mentions it. A Note or Timeline line
>   may also link to one inline (`[name](assets/name)`) — that means the file
>   is directly relevant to what's being said at that timestamp."""


# ── device discovery ────────────────────────────────────────────────────────
# Dispatch on OS: Linux uses PipeWire/PulseAudio (pactl/parec); Windows uses
# soundcard (WASAPI). The dict shape is identical on both paths:
# {"name": str, "desc": str, "monitor": bool}, and StreamWorker.source stays a
# plain string, so the GUI (mic-vs-speaker split, palettes) is untouched.

# Why the last device listing failed, if it did, so the GUI can show it rather
# than just presenting an empty device list.
device_error: str | None = None


def get_sources() -> list[dict]:
    return _get_sources_windows() if IS_WINDOWS else _get_sources_linux()


def get_defaults() -> tuple[str, str]:
    return _get_defaults_windows() if IS_WINDOWS else _get_defaults_linux()


# ── Windows backend (soundcard / WASAPI) ────────────────────────────────────
# soundcard talks to WASAPI directly and supports loopback capture of an output
# device (system audio), which PortAudio/sounddevice can't do. Sources are
# "in:<id>" for mics and "out:<id>" for a speaker's loopback, where <id> is the
# WASAPI endpoint id.
#
# soundcard initialises COM (multithreaded) on whichever thread first imports
# it. tkinterdnd2 needs the GUI thread to stay single-threaded COM for OLE drag
# and drop, so soundcard is only ever imported and used off the GUI thread.

def _soundcard():
    """Import soundcard and join this thread to the multithreaded COM apartment.

    The import must come first: soundcard's import-time CoInitializeEx treats
    S_FALSE ("already initialised on this thread") as an error and fails.
    """
    import ctypes
    import soundcard
    ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    return soundcard


def _off_gui_thread(fn):
    result: list = []

    def run():
        try:
            result.append((True, fn()))
        except Exception as e:
            result.append((False, e))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=10)
    if not result:
        raise TimeoutError("audio device query timed out")
    ok, value = result[0]
    if not ok:
        raise value
    return value


def _get_sources_windows() -> list[dict]:
    global device_error

    def query():
        sc = _soundcard()
        mics = [{"name": f"in:{m.id}", "desc": m.name, "monitor": False}
                for m in sc.all_microphones()]
        spks = [{"name": f"out:{s.id}", "desc": f"{s.name}  (system audio)", "monitor": True}
                for s in sc.all_speakers()]
        return mics + spks

    try:
        sources = _off_gui_thread(query)
    except Exception as e:
        device_error = f"Could not list audio devices: {e!r}"
        return []
    device_error = None
    return sources


def _get_defaults_windows() -> tuple[str, str]:
    def query():
        sc = _soundcard()
        mic, spk = sc.default_microphone(), sc.default_speaker()
        return (f"in:{mic.id}" if mic else "", f"out:{spk.id}" if spk else "")

    try:
        return _off_gui_thread(query)
    except Exception:
        return "", ""


# ── Linux backend (PipeWire via pactl) ──────────────────────────────────────

def _get_sources_linux() -> list[dict]:
    """Every PipeWire source: real mics (incl. Bluetooth) and sink monitors (system audio)."""
    try:
        out = subprocess.run(
            ["pactl", "list", "sources"], capture_output=True, text=True, check=True
        ).stdout
    except Exception:
        return []
    sources, name = [], None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line[len("Name:"):].strip()
        elif line.startswith("Description:") and name:
            is_monitor = name.endswith(".monitor")
            desc = line[len("Description:"):].strip()
            if is_monitor and desc.startswith("Monitor of "):
                desc = desc[len("Monitor of "):] + "  (system audio)"
            sources.append({"name": name, "desc": desc, "monitor": is_monitor})
            name = None
    return sources


def _get_defaults_linux() -> tuple[str, str]:
    """(default source name, default sink monitor name)."""
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True, check=True
        ).stdout.strip()
        source = subprocess.run(
            ["pactl", "get-default-source"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return "", ""
    return source, f"{sink}.monitor"


# ── audio / transcription worker ────────────────────────────────────────────────

class StreamWorker:
    """Captures one PipeWire source (via parec) and pushes transcribed lines into `out_q`."""

    def __init__(self, source: str, label: str, model: WhisperModel, out_q: queue.Queue):
        self.source = source
        self.label = label
        self.model = model
        self.out_q = out_q
        self._stop_event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._buf = np.empty(0, dtype=np.float32)
        # One transcriber thread per stream with a bounded queue: lines stay in
        # order, and a CPU that can't keep up drops old audio instead of piling
        # up threads and lagging further behind.
        self._chunks: queue.Queue[np.ndarray] = queue.Queue(maxsize=MAX_BACKLOG)
        self._warned_backlog = False
        self.level = 0.0  # RMS of the latest captured audio, read by the GUI's level meter

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._transcribe_loop, daemon=True).start()

    def _run(self):
        (self._run_windows if IS_WINDOWS else self._run_linux)()

    def stop(self):
        self._stop_event.set()
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass

    def _run_windows(self):
        kind, _, dev_id = self.source.partition(":")
        try:
            sc = _soundcard()
            # A speaker's loopback is opened as a "microphone" on the same endpoint.
            dev = sc.get_microphone(dev_id, include_loopback=(kind == "out"))
            # WASAPI resamples to 16 kHz for us; downmix to mono below.
            rec = dev.recorder(samplerate=SAMPLE_RATE, channels=dev.channels)
        except Exception as e:
            self.out_q.put(("ERR", f"[{self.label}] device error: {e}"))
            return

        try:
            with rec:
                while not self._stop_event.is_set():
                    data = rec.record(numframes=SAMPLE_RATE // 10)
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    self._feed(mono.astype(np.float32), SAMPLE_RATE)
        except Exception as e:
            self.out_q.put(("ERR", f"[{self.label}] stream error: {e}"))

    def _run_linux(self):
        cmd = [
            "parec", f"--device={self.source}",
            "--rate", str(SAMPLE_RATE), "--channels", "1",
            "--format", "float32le", "--raw", "--latency-msec=50",
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as e:
            self.out_q.put(("ERR", f"[{self.label}] stream error: {e}"))
            return

        leftover = b""  # a read can end mid-sample
        while not self._stop_event.is_set():
            raw = self._proc.stdout.read1(65536)
            if not raw:
                break
            raw = leftover + raw
            usable = len(raw) - len(raw) % 4
            leftover = raw[usable:]
            if usable:
                self._feed(np.frombuffer(raw[:usable], dtype=np.float32), SAMPLE_RATE)

    def _feed(self, samples: np.ndarray, rate: int):
        """Append captured audio and queue every complete chunk it now holds."""
        if samples.size:
            self.level = float(np.sqrt(np.mean(samples ** 2)))
        self._buf = np.concatenate((self._buf, samples))
        while (cut := _find_cut(self._buf, rate)) is not None:
            self._enqueue(self._buf[:cut].copy())
            self._buf = self._buf[cut:]

    def _enqueue(self, chunk: np.ndarray):
        try:
            self._chunks.put_nowait(chunk)
            return
        except queue.Full:
            pass
        try:
            self._chunks.get_nowait()  # drop the oldest so the transcript stays live
        except queue.Empty:
            pass
        self._chunks.put_nowait(chunk)
        if not self._warned_backlog:
            self._warned_backlog = True
            self.out_q.put(("ERR", f"[{self.label}] CPU can't keep up — skipping some audio. "
                                   "Try a smaller model or fewer sources."))

    def _transcribe_loop(self):
        while not self._stop_event.is_set():
            try:
                chunk = self._chunks.get(timeout=0.5)
            except queue.Empty:
                continue
            self._transcribe(chunk)

    def _transcribe(self, chunk: np.ndarray):
        if self._stop_event.is_set():
            return
        if float(np.sqrt(np.mean(chunk**2))) < SILENCE_RMS:
            return
        try:
            # Greedy decoding (beam_size=1) is ~2-3x faster than the default
            # beam of 5 for a small accuracy cost — worth it for live captions.
            segments, _ = self.model.transcribe(chunk, beam_size=1, vad_filter=True)
            text = " ".join(s.text for s in segments).strip()
        except Exception as e:
            self.out_q.put(("ERR", f"[{self.label}] transcribe error: {e}"))
            return
        if not text:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{self.label}] {text}"
        self.out_q.put((self.label, line))


def _bundled_model_path(name: str) -> Path | None:
    path = BUNDLED_MODELS_DIR / name
    return path if (path / "model.bin").is_file() else None


def _find_cut(buf: np.ndarray, rate: int) -> int | None:
    """Where to end the next chunk of `buf`, or None to wait for more audio.

    Cuts mid-way through the first pause after MIN_CHUNK_SEC; with no pause,
    at the quietest frame once MAX_CHUNK_SEC of audio has built up.
    """
    lo, hi = MIN_CHUNK_SEC * rate, MAX_CHUNK_SEC * rate
    frame = int(rate * PAUSE_FRAME_SEC)
    n = (min(len(buf), hi) - lo) // frame
    if n <= 0:
        return None
    frames = buf[lo:lo + n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    level = float(np.sqrt(np.mean(buf[:lo + n * frame] ** 2)))
    quiet = np.flatnonzero(rms < max(PAUSE_RATIO * level, SILENCE_RMS))
    if quiet.size:
        i = int(quiet[0])
    elif len(buf) >= hi:
        i = int(np.argmin(rms))
    else:
        return None
    return lo + i * frame + frame // 2


# ── model-download progress hook ─────────────────────────────────────────────
# faster-whisper downloads the CTranslate2 snapshot via huggingface_hub's
# snapshot_download on first use of a given size — but it *deliberately* passes
# its own `faster_whisper.utils.disabled_tqdm` (which forces `disable=True`) as
# the `tqdm_class`, so no progress is normally shown. snapshot_download builds the
# aggregate "bytes downloaded" bar from exactly that class, so we swap
# `faster_whisper.utils.disabled_tqdm` for a subclass that reports progress into a
# queue. `download_model` reads that name from module globals at call time, so the
# patch takes effect for the next WhisperModel load.
#
# The subclass tracks its own byte counter instead of relying on the base bar's
# `self.n`: with no TTY (a windowed GUI) the bar ends up disabled and tqdm's own
# `update()` never advances `n`.

_dl_state: dict[int, tuple[int, int]] = {}
_dl_target_queue: queue.Queue | None = None


def _install_download_progress_hook(target_queue: queue.Queue) -> None:
    """Monkeypatch faster-whisper's download tqdm so model downloads report into target_queue."""
    global _dl_target_queue
    _dl_target_queue = target_queue
    import faster_whisper.utils as fwu
    if getattr(fwu, "_gui_patched", False):
        return
    base_tqdm = fwu.disabled_tqdm.__bases__[0]  # the real tqdm (tqdm.auto.tqdm)

    class _GuiTqdm(base_tqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # Only the byte-download bar (unit="B") is progress we care about;
            # snapshot_download also builds an outer "Fetching N files" counter
            # bar from this same class — ignore it so cache hits (no byte bar)
            # never flash the GUI bar and the byte total isn't skewed by +N files.
            self._gui_track = getattr(self, "unit", None) == "B"
            if not self._gui_track:
                return
            self._gui_done = self.n or 0
            _dl_state[id(self)] = (self._gui_done, self.total or 0)
            self._emit()

        def update(self, n=1):
            if self._gui_track:
                self._gui_done += n or 0
                _dl_state[id(self)] = (self._gui_done, self.total or 0)
                self._emit()
            return super().update(n)

        def close(self):
            if self._gui_track:
                _dl_state.pop(id(self), None)
                self._emit()
            super().close()

        def _emit(self):
            if _dl_target_queue is not None:
                done = sum(n for n, _ in _dl_state.values())
                total = sum(t for _, t in _dl_state.values())
                _dl_target_queue.put((done, total))

    fwu.disabled_tqdm = _GuiTqdm
    fwu._gui_patched = True


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tb.Window, TkinterDnD.DnDWrapper):
    def __init__(self):
        super().__init__(title="Transcriber", themename="darkly", minsize=(780, 620))
        self.TkdndVersion = TkinterDnD._require(self)

        self._sources = get_sources()
        self._mic_sources = [s for s in self._sources if not s["monitor"]]
        self._spk_sources = [s for s in self._sources if s["monitor"]]
        self._mic_vars: dict[str, tk.BooleanVar] = {}
        self._spk_vars: dict[str, tk.BooleanVar] = {}
        self._label_colors: dict[str, str] = {}
        self._mic_color_idx = 0
        self._spk_color_idx = 0
        self._workers: list[StreamWorker] = []
        self._q: queue.Queue = queue.Queue()
        self._dl_queue: queue.Queue = queue.Queue()
        self._model: WhisperModel | None = None
        self._loading = False
        self._connecting = False

        # session: notes, attachments, and the combined timeline exported to markdown
        self._session_dir: Path | None = None
        self._assets_dir: Path | None = None
        self._attachments: dict[str, Path] = {}
        self._events: list[dict] = []
        self._suggest_win: tk.Toplevel | None = None
        self._suggest_list: tk.Listbox | None = None
        self._suggest_start = 0
        self._suggest_end = 0

        self._build()
        self._poll()
        self.after(150, self._poll_downloads)

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self):
        pad = {"padx": 16, "pady": 6}

        top_bar = tb.Frame(self)
        top_bar.pack(fill="x", padx=16, pady=(16, 6))
        tb.Label(top_bar, text="🎙  Transcriber", font=("", 18, "bold")).pack(side="left")
        tb.Button(
            top_bar, text="🔴  Track All", bootstyle="danger", command=self._track_all
        ).pack(side="right")
        tb.Label(
            top_bar, text="ticks every mic, Bluetooth device & speaker, then starts",
            bootstyle="secondary"
        ).pack(side="right", padx=10)

        # Device frame
        dev_frame = tb.Labelframe(self, text="Audio devices", bootstyle="secondary", padding=14)
        dev_frame.pack(fill="x", **pad)

        tb.Label(dev_frame, text="Microphones (tick to use):").grid(
            row=0, column=0, sticky="nw"
        )
        # ponytail: no scrollbar for the device lists, fine for a handful of devices
        self._mic_container = tb.Frame(dev_frame)
        self._mic_container.grid(row=0, column=1, padx=8, pady=4, sticky="ew")
        self._build_mic_checks()

        tb.Label(dev_frame, text="Speakers / system audio (tick to use):").grid(
            row=1, column=0, sticky="nw", pady=(10, 0)
        )
        self._spk_container = tb.Frame(dev_frame)
        self._spk_container.grid(row=1, column=1, padx=8, pady=(10, 4), sticky="ew")
        self._build_spk_checks()

        tb.Button(
            dev_frame, text="⟳  Refresh", bootstyle="secondary-outline",
            command=self._refresh_devices
        ).grid(row=0, column=2, sticky="n", padx=8)
        dev_frame.columnconfigure(1, weight=1)

        self._preselect()
        self.after(0, self._report_device_error)

        # Settings frame
        cfg_frame = tb.Labelframe(self, text="Settings", bootstyle="secondary", padding=14)
        cfg_frame.pack(fill="x", **pad)

        tb.Label(cfg_frame, text="Model:").grid(row=0, column=0, sticky="w")
        self._model_var = tk.StringVar(value="base")  # the size the Windows build bundles
        tb.Combobox(
            cfg_frame, textvariable=self._model_var, bootstyle="info",
            values=["tiny", "base", "small", "medium", "large-v3"],
            width=12, state="readonly",
        ).grid(row=0, column=1, padx=8, sticky="w")
        tb.Label(cfg_frame, text="  tiny=fastest  base=good  small=better  medium=best",
                 bootstyle="secondary").grid(row=0, column=2, sticky="w")
        cfg_frame.columnconfigure(2, weight=1)

        # Model-download progress — a permanently-slotted frame (fixed in the pack
        # order) whose Floodgauge is packed/unpacked on demand. Nesting it here keeps
        # the toggle from disturbing the outer pack order (log_frame's expand=True
        # would otherwise swallow a bare sibling toggled at the top level).
        dl_frame = tb.Frame(self)
        dl_frame.pack(fill="x", padx=16, pady=(0, 6))
        self._dl_bar = tb.Floodgauge(dl_frame, bootstyle="info", mode="determinate", maximum=100)

        # Attachments — drag & drop images, PDFs, or any file; referenced from notes as /filename
        attach_frame = tb.Labelframe(
            self, text="Attachments — drag & drop files here, or click Add",
            bootstyle="secondary", padding=14,
        )
        attach_frame.pack(fill="x", **pad)

        # ponytail: plain tk.Listbox, no scrollbar — fine for a handful of attachments
        self._attach_list = tk.Listbox(attach_frame, height=3, activestyle="none", exportselection=False)
        self._attach_list.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tb.Button(
            attach_frame, text="＋  Add File…", bootstyle="secondary-outline",
            command=self._add_files_dialog
        ).pack(side="right", anchor="n")

        for w in (attach_frame, self._attach_list):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)

        # Start / Stop / Export
        btn_bar = tb.Frame(self)
        btn_bar.pack(pady=10)
        self._btn = tb.Button(
            btn_bar, text="▶  Start", command=self._toggle, width=14, bootstyle="success"
        )
        self._btn.pack(side="left", padx=4)
        self._save_btn = tb.Button(
            btn_bar, text="📄  Export Notes (.md)", bootstyle="info-outline",
            command=self._export_markdown
        )
        self._save_btn.pack(side="left", padx=4)
        self._status = tb.Label(btn_bar, text="", bootstyle="secondary")
        self._status.pack(side="left", padx=10)

        # Live input-level meters, one row per recording source — slotted in the
        # pack order now and filled on Start, like dl_frame above.
        self._meter_frame = tb.Frame(self)
        self._meter_frame.pack(fill="x", padx=16)
        self._meters: list[tuple[StreamWorker, tb.Progressbar]] = []
        self._meter_levels: list[float] = []
        self._meter_job: str | None = None

        # Notes — packed to the bottom edge *before* the expanding Transcript frame below,
        # so it keeps a fixed height instead of being squeezed out by log_frame's expand=True.
        notes_frame = tb.Labelframe(
            self, text="Notes — add a timestamped comment (type / to reference an attachment)",
            bootstyle="secondary", padding=10,
        )
        notes_frame.pack(side="bottom", fill="x", padx=16, pady=(0, 16))

        note_bar = tb.Frame(notes_frame)
        note_bar.pack(fill="x")
        self._note_var = tk.StringVar()
        self._note_entry = tb.Entry(note_bar, textvariable=self._note_var)
        self._note_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tb.Button(note_bar, text="Add Note", bootstyle="info", command=self._add_note).pack(side="left")

        self._note_entry.bind("<KeyRelease>", self._on_note_key)
        self._note_entry.bind("<Return>", self._on_note_return)
        self._note_entry.bind("<Tab>", self._on_note_tab)
        self._note_entry.bind("<Up>", self._on_note_up)
        self._note_entry.bind("<Down>", self._on_note_down)
        self._note_entry.bind("<Escape>", lambda e: self._hide_suggest())
        self._note_entry.bind("<FocusOut>", lambda e: self.after(150, self._hide_suggest))

        # Transcript
        log_frame = tb.Labelframe(self, text="Transcript", bootstyle="secondary", padding=10)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        self._scroller = ScrolledText(
            log_frame, autohide=True, wrap="word", font=("Monospace", 10),
        )
        self._scroller.pack(fill="both", expand=True)
        self._txt = self._scroller.text
        self._txt.config(state="disabled")
        self._txt.tag_config("ERR", foreground="#ef5350")
        self._txt.tag_config("SYS", foreground="#888888")
        self._txt.tag_config("NOTE", foreground="#ffd54f")

        tb.Button(
            log_frame, text="Clear", bootstyle="secondary-outline", command=self._clear
        ).pack(anchor="e", pady=(8, 0))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _build_mic_checks(self):
        for w in self._mic_container.winfo_children():
            w.destroy()
        self._mic_vars = {}
        for s in self._mic_sources:
            var = tk.BooleanVar(value=False)
            tb.Checkbutton(
                self._mic_container, text=s["desc"], variable=var, bootstyle="info-round-toggle"
            ).pack(anchor="w", pady=2)
            self._mic_vars[s["name"]] = var

    def _build_spk_checks(self):
        for w in self._spk_container.winfo_children():
            w.destroy()
        self._spk_vars = {}
        for s in self._spk_sources:
            var = tk.BooleanVar(value=False)
            tb.Checkbutton(
                self._spk_container, text=s["desc"], variable=var,
                bootstyle="success-round-toggle"
            ).pack(anchor="w", pady=2)
            self._spk_vars[s["name"]] = var

    def _preselect(self):
        default_mic, default_spk_monitor = get_defaults()
        if default_mic in self._mic_vars:
            self._mic_vars[default_mic].set(True)
        elif self._mic_sources:
            self._mic_vars[self._mic_sources[0]["name"]].set(True)
        if default_spk_monitor in self._spk_vars:
            self._spk_vars[default_spk_monitor].set(True)
        elif self._spk_sources:
            self._spk_vars[self._spk_sources[0]["name"]].set(True)

    def _refresh_devices(self):
        self._sources = get_sources()
        self._mic_sources = [s for s in self._sources if not s["monitor"]]
        self._spk_sources = [s for s in self._sources if s["monitor"]]
        self._build_mic_checks()
        self._build_spk_checks()
        self._preselect()
        self._log("Device list refreshed.", "SYS")
        self._report_device_error()

    def _report_device_error(self):
        if device_error:
            self._log(device_error, "ERR")

    def _track_all(self):
        for var in self._mic_vars.values():
            var.set(True)
        for var in self._spk_vars.values():
            var.set(True)
        if not self._workers:
            self._start()

    def _tag_for(self, label: str, palette: list[str], is_mic: bool) -> str:
        if label in self._label_colors:
            return label
        if is_mic:
            color = palette[self._mic_color_idx % len(palette)]
            self._mic_color_idx += 1
        else:
            color = palette[self._spk_color_idx % len(palette)]
            self._spk_color_idx += 1
        self._label_colors[label] = color
        self._txt.tag_config(label, foreground=color)
        return label

    def _log(self, text: str, tag: str):
        self._txt.config(state="normal")
        self._txt.insert("end", text + "\n", tag)
        self._txt.see("end")
        self._txt.config(state="disabled")

    def _clear(self):
        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")
        self._txt.config(state="disabled")
        self._events.clear()

    def _set_status(self, text: str):
        self._status.config(text=text)

    # ── session / assets ─────────────────────────────────────────────────────

    def _ensure_session(self) -> Path:
        """Lazily create sessions/session_<timestamp>/assets/ on first note, attachment, or start."""
        if self._session_dir is None:
            self._session_dir = SESSIONS_DIR / f"session_{datetime.datetime.now():%Y%m%d_%H%M%S}"
            self._assets_dir = self._session_dir / "assets"
            self._assets_dir.mkdir(parents=True, exist_ok=True)
            self._log(f"Session folder: {self._session_dir}", "SYS")
        return self._session_dir

    def _unique_asset_name(self, name: str) -> str:
        if name not in self._attachments:
            return name
        stem, suffix = Path(name).stem, Path(name).suffix
        i = 2
        while f"{stem} ({i}){suffix}" in self._attachments:
            i += 1
        return f"{stem} ({i}){suffix}"

    def _add_files_dialog(self):
        paths = filedialog.askopenfilenames(title="Add attachment(s)")
        if paths:
            self._add_attachments(paths)

    def _on_drop(self, event):
        self._add_attachments(self.tk.splitlist(event.data))

    def _add_attachments(self, paths):
        self._ensure_session()
        for raw in paths:
            src = Path(raw)
            if not src.is_file():
                continue
            name = self._unique_asset_name(src.name)
            try:
                shutil.copy2(src, self._assets_dir / name)
            except OSError as e:
                self._log(f"Could not attach {src.name}: {e}", "ERR")
                continue
            self._attachments[name] = self._assets_dir / name
            self._attach_list.insert("end", name)
            self._log(f"Attached: {name}  (reference it in a note as /{name})", "SYS")
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            link = f"[{name}](assets/{quote(name)})"
            self._events.append({"ts": ts, "kind": "attachment", "label": "📎 Attachment", "text": link})

    # ── notes + /filename autocomplete ───────────────────────────────────────

    def _add_note(self):
        text = self._note_var.get().strip()
        if not text:
            return
        self._hide_suggest()
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._events.append({"ts": ts, "kind": "note", "label": "Note", "text": text})
        self._log(f"[{ts}] [Note] {text}", "NOTE")
        self._note_var.set("")

    def _on_note_return(self, event):
        if self._suggest_list is not None:
            self._accept_suggestion()
        else:
            self._add_note()
        return "break"

    def _on_note_tab(self, event):
        if self._suggest_list is not None:
            self._accept_suggestion()
            return "break"
        return None

    def _on_note_up(self, event):
        if self._suggest_list is not None:
            self._move_suggest(-1)
            return "break"
        return None

    def _on_note_down(self, event):
        if self._suggest_list is not None:
            self._move_suggest(1)
            return "break"
        return None

    def _on_note_key(self, event):
        if event.keysym in NOTE_KEY_IGNORE:
            return
        text = self._note_var.get()
        cursor = self._note_entry.index(tk.INSERT)
        m = re.search(r"/([^\s/]*)$", text[:cursor])
        if not m or not self._attachments:
            self._hide_suggest()
            return
        query = m.group(1).lower()
        matches = [n for n in self._attachments if n.lower().startswith(query)]
        if not matches:
            self._hide_suggest()
            return
        self._suggest_start, self._suggest_end = m.start(), cursor
        self._show_suggest(matches)

    def _show_suggest(self, matches: list[str]):
        if self._suggest_win is None:
            win = tk.Toplevel(self)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            lb = tk.Listbox(win, activestyle="none", exportselection=False)
            lb.pack(fill="both", expand=True)
            lb.bind("<ButtonRelease-1>", lambda e: self._accept_suggestion())
            self._suggest_win, self._suggest_list = win, lb

        self._suggest_list.config(height=min(6, len(matches)))
        self._suggest_list.delete(0, "end")
        for name in matches:
            self._suggest_list.insert("end", name)
        self._suggest_list.selection_set(0)

        x = self._note_entry.winfo_rootx()
        y = self._note_entry.winfo_rooty() + self._note_entry.winfo_height()
        self._suggest_win.geometry(f"+{x}+{y}")
        self._suggest_win.deiconify()

    def _move_suggest(self, delta: int):
        size = self._suggest_list.size()
        cur = self._suggest_list.curselection()
        idx = ((cur[0] + delta) % size) if cur else 0
        self._suggest_list.selection_clear(0, "end")
        self._suggest_list.selection_set(idx)
        self._suggest_list.activate(idx)

    def _accept_suggestion(self):
        sel = self._suggest_list.curselection()
        if not sel:
            self._hide_suggest()
            return
        name = self._suggest_list.get(sel[0])
        text = self._note_var.get()
        new_text = f"{text[:self._suggest_start]}/{name} {text[self._suggest_end:]}"
        self._note_var.set(new_text)
        self._note_entry.icursor(self._suggest_start + len(name) + 2)
        self._hide_suggest()
        self._note_entry.focus_set()

    def _hide_suggest(self):
        if self._suggest_win is not None:
            self._suggest_win.destroy()
            self._suggest_win = None
            self._suggest_list = None

    # ── markdown export ──────────────────────────────────────────────────────

    def _export_markdown(self, silent: bool = False) -> Path | None:
        if not self._events and not self._attachments:
            if not silent:
                self._log("Nothing to export yet.", "SYS")
            return None

        session_dir = self._ensure_session()
        names_by_length = sorted(self._attachments, key=len, reverse=True)

        lines = [f"# Transcript — {datetime.datetime.now():%Y-%m-%d %H:%M}", "", MD_PREAMBLE, ""]
        if self._attachments:
            lines.append("## Attachments")
            lines += [f"- [{name}](assets/{quote(name)})" for name in self._attachments]
            lines.append("")

        lines.append("## Timeline")
        for ev in self._events:
            text = ev["text"]
            for name in names_by_length:
                text = text.replace(f"/{name}", f"[{name}](assets/{quote(name)})")
            who = "📝 Note" if ev["kind"] == "note" else ev["label"]
            lines.append(f"- **{ev['ts']}** {who} — {text}")
        lines.append("")

        md_path = session_dir / "transcript.md"
        # Explicit UTF-8: Windows defaults to cp1252, which can't encode the
        # 📝/📎 emoji used above.
        try:
            md_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as e:
            self._log(f"Could not export {md_path}: {e}", "ERR")
            return None
        self._log(f"Exported: {md_path}", "SYS")
        return md_path

    # ── start / stop ──────────────────────────────────────────────────────────

    def _toggle(self):
        if self._workers:
            self._stop()
        else:
            self._start()

    def _start(self):
        mic_srcs = [name for name, var in self._mic_vars.items() if var.get()]
        spk_srcs = [name for name, var in self._spk_vars.items() if var.get()]
        if not mic_srcs and not spk_srcs:
            self._log("Tick at least one microphone or speaker device.", "ERR")
            return

        desc_by_name = {s["name"]: s["desc"] for s in self._sources}
        mic_labels = {name: desc_by_name[name] for name in mic_srcs}
        spk_labels = {name: desc_by_name[name] for name in spk_srcs}
        model_name = self._model_var.get()

        self._btn.config(state="disabled")
        self._set_status(f"Loading '{model_name}' model…")
        self._loading = True
        # If nothing (cache hit) has resolved this quickly, the model is likely
        # downloading — but real per-file byte progress only starts once the
        # connection is established. Give visible feedback before that, since a
        # slow/blocked connection (flaky wifi, VPN, antivirus TLS inspection)
        # can stall there for a long time with zero signal otherwise.
        self.after(1500, lambda: self._maybe_start_connecting(model_name))

        def load():
            try:
                # Re-use cached model when name hasn't changed
                if self._model is None or getattr(self._model, "_name", "") != model_name:
                    _install_download_progress_hook(self._dl_queue)
                    bundled = _bundled_model_path(model_name)
                    m = WhisperModel(str(bundled) if bundled else model_name,
                                     device="cpu", compute_type="int8")
                    m._name = model_name  # type: ignore[attr-defined]
                    self._model = m
            except Exception as e:
                self.after(0, lambda: self._load_failed(str(e)))
                return
            self.after(0, lambda: self._launch(mic_srcs, mic_labels, spk_srcs, spk_labels))

        threading.Thread(target=load, daemon=True).start()

    def _load_failed(self, message: str):
        self._loading = False
        self._stop_connecting_indicator()
        self._dl_bar.pack_forget()
        self._btn.config(state="normal", text="▶  Start", bootstyle="success")
        self._set_status("")
        self._log(f"Could not load model: {message}", "ERR")

    def _launch(
        self,
        mic_srcs: list[str],
        mic_labels: dict[str, str],
        spk_srcs: list[str],
        spk_labels: dict[str, str],
    ):
        self._loading = False
        self._stop_connecting_indicator()

        pairs = [(src, mic_labels[src], MIC_PALETTE, True) for src in mic_srcs]
        pairs += [(src, spk_labels[src], SPEAKER_PALETTE, False) for src in spk_srcs]
        for src, label, palette, is_mic in pairs:
            self._tag_for(label, palette, is_mic)
            w = StreamWorker(src, label, self._model, self._q)
            w.start()
            self._workers.append(w)
            self._add_meter(w, is_mic)

        self._btn.config(state="normal", text="■  Stop", bootstyle="danger")
        self.title("● Recording — Transcriber")
        self._log("Started — speak or play audio.", "SYS")
        self._tick_meters()

    def _stop(self):
        for w in self._workers:
            w.stop()
        self._workers.clear()
        self._clear_meters()
        self._btn.config(text="▶  Start", bootstyle="success")
        self.title("Transcriber")
        self._status.config(bootstyle="secondary")
        self._set_status("")
        self._log("Stopped.", "SYS")
        self._export_markdown(silent=True)

    # ── recording indicator + level meters ──────────────────────────────────

    def _add_meter(self, worker: StreamWorker, is_mic: bool):
        row = tb.Frame(self._meter_frame)
        row.pack(fill="x", pady=1)
        tb.Label(row, text=("🎤 " if is_mic else "🔊 ") + worker.label,
                 width=40, anchor="w").pack(side="left")
        bar = tb.Progressbar(row, bootstyle="success", maximum=100, value=0)
        bar.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self._meters.append((worker, bar))
        self._meter_levels.append(0.0)

    def _clear_meters(self):
        if self._meter_job is not None:
            self.after_cancel(self._meter_job)
            self._meter_job = None
        for child in self._meter_frame.winfo_children():
            child.destroy()
        self._meter_frame.configure(height=1)  # an emptied frame otherwise keeps its old height
        self._meters.clear()
        self._meter_levels.clear()

    def _tick_meters(self):
        if not self._workers:
            return
        for i, (worker, bar) in enumerate(self._meters):
            # dBFS mapped onto 0-100 over a -60..0 dB range; fast attack, slow
            # decay so speech reads as a steady bar rather than flicker.
            db = 20 * np.log10(worker.level + 1e-9)
            pct = float(np.clip((db + 60) / 60 * 100, 0, 100))
            self._meter_levels[i] = max(pct, self._meter_levels[i] * 0.8)
            bar.configure(value=self._meter_levels[i])
        # Blink the status twice a second: a clear "this is recording" cue.
        on = int(time.monotonic() * 2) % 2 == 0
        self._status.config(text="● REC" if on else "○ REC", bootstyle="danger")
        self._meter_job = self.after(100, self._tick_meters)

    # ── queue poll ────────────────────────────────────────────────────────────

    _LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[.*?\] (.*)$")

    def _poll(self):
        try:
            while True:
                label, text = self._q.get_nowait()
                self._log(text, label)
                if label != "ERR":
                    m = self._LINE_RE.match(text)
                    if m:
                        self._events.append(
                            {"ts": m.group(1), "kind": "line", "label": label, "text": m.group(2)}
                        )
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _poll_downloads(self):
        try:
            while True:
                done, total = self._dl_queue.get_nowait()
                if total:
                    self._stop_connecting_indicator()  # real byte progress beats the "connecting" guess
                    pct = min(100, done * 100 / total)
                    if not self._dl_bar.winfo_ismapped():
                        self._dl_bar.pack(fill="x")
                    self._dl_bar["value"] = pct
                    self._set_status(
                        f"Downloading model… {done/1e6:.1f}/{total/1e6:.1f} MB ({pct:.0f}%)"
                    )
                    if done >= total:
                        self.after(800, self._dl_bar.pack_forget)
        except queue.Empty:
            pass
        self.after(150, self._poll_downloads)

    def _maybe_start_connecting(self, model_name: str):
        if self._loading and not self._dl_bar.winfo_ismapped():
            self._connecting = True
            self._connect_started = time.monotonic()
            self._dl_bar.mode = "indeterminate"  # a plain attribute on this widget, not a Tk option
            self._dl_bar.start()
            self._dl_bar.pack(fill="x")
            self._tick_connecting(model_name)

    def _tick_connecting(self, model_name: str):
        if not self._connecting:
            return
        elapsed = int(time.monotonic() - self._connect_started)
        self._set_status(
            f"Loading '{model_name}' model… still working ({elapsed}s) — "
            "if this hangs, check that a VPN or antivirus isn't blocking the connection"
        )
        self.after(1000, lambda: self._tick_connecting(model_name))

    def _stop_connecting_indicator(self):
        if self._connecting:
            self._connecting = False
            self._dl_bar.stop()
            self._dl_bar.mode = "determinate"


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
