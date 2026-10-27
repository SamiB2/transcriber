# Transcriber

Free, fully local, dual-stream live transcription for meetings and calls — no
cloud, no API key, nothing uploaded anywhere. Speech-to-text runs entirely on
your CPU with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
(Whisper), so your audio never leaves the machine.

It captures **two kinds of audio at once and labels them separately**:

- your **microphone(s)** (including Bluetooth headset mics), and
- the **system / meeting audio** — whatever is actually playing out of your
  speakers or headset (the other people in a Zoom/Teams/Meet/Preply call, a
  video, etc.).

Both streams are transcribed in real time into one timestamped, colour-coded
transcript, so you can see who-said-what across the mic and the call.

## Features

- **Dual-stream capture** — tick any combination of mics and speakers/outputs;
  each source gets its own label and colour in the transcript. "🔴 Track All"
  ticks everything and starts in one click.
- **Timestamped notes** — type a note during the session and it's inserted into
  the timeline with a timestamp, marked as higher-signal than the raw ASR text.
- **Drag-and-drop attachments** — drop images, PDFs, or any file onto the
  window (or click *Add File…*). They're copied into the session's `assets/`
  folder, and you can reference one from a note by typing `/` and picking it
  from the autocomplete (`/screenshot.png`).
- **Auto-exported markdown** — every session writes a
  `sessions/session_<timestamp>/transcript.md` (plus an `assets/` folder). The
  file leads with a short explainer aimed at AI/note-building tools: the
  timeline is raw, unedited ASR meant as *source material* for building a clean
  notes document — Note lines are flagged as higher-signal, and attachments are
  linked inline where they're relevant. Feed the whole folder to an AI to turn
  a messy live session into tidy notes.
- **Model picker** — choose Whisper size (`tiny` … `large-v3`) for a
  speed/accuracy trade-off. The first use of a size downloads it once (with a
  live progress bar) and caches it locally for later.

## Requirements

- Python 3.12
- A few Python packages (see `requirements.txt`): `faster-whisper`, `numpy`,
  `ttkbootstrap`, `tkinterdnd2`, and — on Windows only — `sounddevice`.

### Linux (validated)

Linux captures audio through PipeWire/PulseAudio using the `pactl`/`parec` CLI
tools, which is why it sees every real source: physical mics, Bluetooth mics,
and the *monitor* of any output (speakers/headset) for system audio.

```bash
# system packages (once)
sudo apt install python3.12-venv python3-tk pulseaudio-utils -y

# project setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python transcribe.py
```

### Windows

Windows capture uses [`sounddevice`](https://python-sounddevice.readthedocs.io/)
(PortAudio, WASAPI). System audio is captured by opening an output device in
loopback mode; microphones are captured directly. See *Known limitations* below.

**Option A — prebuilt executable:** grab `Transcriber.exe` from the latest
[GitHub Actions "Build Windows executable" run](../../actions) (Artifacts
section) and run it. No Python install needed. The `small` Whisper model is
bundled inside the exe, so it works offline right away with no first-run
download (other sizes still download on first use).

**Option B — from source:**

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python transcribe.py
```

## Usage

1. Launch the app. It lists your microphones and your speakers/outputs.
2. Tick the sources you want (or hit **🔴 Track All**), pick a Whisper model,
   and press **▶ Start**. The first use of a given model size downloads it once.
3. Talk / play the call. Lines appear in the transcript, labelled and coloured
   per source.
4. Type notes in the Notes box as you go; reference a dropped-in file with `/`.
5. Press **■ Stop** (or **📄 Export Notes**) — a `transcript.md` and `assets/`
   folder are written under `sessions/`. Hand that folder to an AI to build
   clean notes.

## Privacy

Whisper runs 100% locally on your CPU. No audio, transcript, or note is ever
uploaded — there's no API key and no network transcription. The only network
access is a one-time download of the chosen Whisper model from Hugging Face,
which is then cached locally.

Your sessions live in `sessions/`, which is **gitignored** and never leaves the
machine.

## Known limitations

- **Windows audio backend is unverified on real hardware.** The Windows capture
  path is implemented against the documented `sounddevice`/PortAudio (WASAPI)
  API and the CI build confirms it imports and bundles cleanly, but it has not
  yet been run on a real Windows machine to confirm mic and system-audio
  (loopback) capture actually work end to end. If something's off on Windows,
  please [open an issue](../../issues). The Linux path is validated.
- No audio resampling: if a device rejects 16 kHz, the app falls back to the
  device's native sample rate, which can affect transcription quality in that
  (rare) case.

## License

MIT — see [LICENSE](LICENSE).
