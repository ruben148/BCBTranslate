# BCBTranslate — Design & Implementation Document

## 1. Overview

BCBTranslate is a real-time speech translation application that captures audio in Romanian, translates it to English via Azure Cognitive Services, and outputs the synthesized English speech to a configurable audio destination. The primary use case is live church translation, where a speaker on a Behringer Midas M32 mixer channel is translated in real time and the English output is routed to a virtual audio device for consumption by Microsoft Teams (or any conferencing tool).

The application must be simple to use, fully configurable, resilient to real-world conditions (fast speakers, network jitter, device changes), and architecturally extensible for future needs.

---

## 2. Problems with the Current Prototypes

| # | Problem | Impact |
|---|---------|--------|
| 1 | Hard-coded to OS default microphone/speaker — no device selection | Cannot target a specific Midas channel or virtual cable without changing Windows defaults every time |
| 2 | No speed or pitch control on TTS output | If the speaker talks fast, the translated English audio falls behind and sounds unnatural |
| 3 | No virtual audio output routing | Cannot feed translated audio into Teams as a "microphone" source |
| 4 | No persistent configuration | Every parameter must be changed in code and restarted |
| 5 | No lag/latency indicator | Operator has no idea if translation is falling behind the speaker |
| 6 | No audio level monitoring | No way to verify the input is actually receiving signal before going live |
| 7 | Midas script is untested and functionally identical to the default-device script | The "Midas" script is aspirational — it does nothing Midas-specific |
| 8 | No graceful error recovery | A transient Azure error kills the session |
| 9 | Single target language hard-coded | Cannot switch to another language pair without editing source |
| 10 | No UI — console only | Not usable by a non-technical operator |

---

## 3. Requirements

### 3.1 Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| F-01 | Enumerate and select any audio **input** device (physical mic, mixer channel, loopback) | Must |
| F-02 | Enumerate and select any audio **output** device (physical speaker, virtual cable) | Must |
| F-03 | Route TTS output to a **virtual audio device** (VB-CABLE, VoiceMeeter, etc.) for use as a Teams mic | Must |
| F-04 | Adjustable TTS **speaking rate** (0.5× – 2.0×) | Must |
| F-05 | Adjustable TTS **pitch** (-50% – +50%) | Must |
| F-06 | Real-time **lag indicator** showing how far behind TTS playback is from live speech | Must |
| F-07 | Persistent **configuration** saved to disk, loaded on startup | Must |
| F-08 | Full **GUI** with intuitive controls | Must |
| F-09 | Selectable **source language** (default: Romanian) | Must |
| F-10 | Selectable **target language** (default: English) | Must |
| F-11 | Selectable **TTS voice** from the Azure voice gallery | Must |
| F-12 | Input **audio level meter** (VU) to confirm signal before going live | Should |
| F-13 | Log panel inside the GUI showing recognized text, translations, and system messages | Should |
| F-14 | **Hotkey** to start/stop translation without focusing the window | Should |
| F-15 | **Profanity filter** toggle (Azure supports this natively) | Should |
| F-16 | **Auto-reconnect** on transient Azure errors | Should |
| F-17 | **TTS queue depth** display alongside the lag indicator | Should |
| F-18 | Option to **log translations** to a timestamped text file (transcript) | Could |
| F-19 | Option to **simultaneously output** to both a virtual device and physical speakers (dual output) | Could |
| F-20 | **Noise suppression** toggle for the input stream | Could |

### 3.2 Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NF-01 | Application must start and be operational within 5 seconds on a modern Windows PC |
| NF-02 | End-to-end latency (speech → translated audio begins playing) must not exceed the Azure service round-trip + 500 ms of local overhead |
| NF-03 | Memory usage must stay under 300 MB during normal operation |
| NF-04 | Must run on Windows 10/11 (primary), with no hard OS-specific dependencies that would prevent a future macOS port |
| NF-05 | All secrets (API keys) must be stored outside the config file, in environment variables or an encrypted local vault |

---

## 4. Architecture

### 4.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────┐
│                        GUI Layer                         │
│  (PyQt6: controls, VU meter, lag display, log panel)     │
└────────────────────────┬─────────────────────────────────┘
                         │  signals / slots
┌────────────────────────▼─────────────────────────────────┐
│                   Application Core                       │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │  Config   │  │  Translation │  │   Audio Router    │  │
│  │  Manager  │  │  Pipeline    │  │                   │  │
│  └──────────┘  └──────┬───────┘  └────────┬──────────┘  │
│                       │                    │              │
│              ┌────────▼────────┐  ┌────────▼──────────┐  │
│              │  Azure Speech   │  │  Device Enumerator│  │
│              │  SDK Wrapper    │  │  (sounddevice /   │  │
│              │                 │  │   PyAudio / WASAPI│  │
│              └─────────────────┘  └───────────────────┘  │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │             Monitoring / Metrics                  │    │
│  │  (lag tracker, queue depth, reconnect logic)      │    │
│  └──────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

### 4.2 Component Breakdown

#### 4.2.1 Configuration Manager (`config_manager.py`)

Responsibilities:
- Load configuration from a JSON file (`bcbtranslate_config.json`) at startup.
- Provide typed, validated access to every configurable parameter.
- Save changes atomically (write to temp file, then rename) to avoid corruption.
- Emit a signal/event when a value changes so other components can react without restart.
- Provide sane defaults for every field so the application works out of the box on first run.

Configuration file location: `%APPDATA%\BCBTranslate\bcbtranslate_config.json`

**Full Configuration Schema:**

```json
{
  "version": 1,

  "azure": {
    "speech_key_env_var": "AZURE_SPEECH_KEY",
    "speech_region_env_var": "AZURE_SPEECH_REGION"
  },

  "translation": {
    "source_language": "ro-RO",
    "target_language": "en",
    "profanity_filter": "masked",
    "continuous_language_detection": false
  },

  "tts": {
    "voice_name": "en-US-JennyNeural",
    "speaking_rate": 1.0,
    "pitch": "+0%",
    "volume": 100
  },

  "audio": {
    "input_device_id": null,
    "output_device_id": null,
    "secondary_output_device_id": null,
    "sample_rate": 16000,
    "channels": 1,
    "noise_suppression": false,
    "input_gain": 1.0,
    "segmentation_silence_timeout_ms": 500
  },

  "ui": {
    "always_on_top": false,
    "start_minimized": false,
    "theme": "dark",
    "hotkey_start_stop": "Ctrl+Shift+T",
    "show_vu_meter": true
  },

  "logging": {
    "log_to_file": false,
    "log_directory": "",
    "log_level": "INFO",
    "save_transcripts": false,
    "transcript_directory": ""
  },

  "advanced": {
    "tts_queue_warning_threshold": 3,
    "max_tts_queue_size": 10,
    "reconnect_attempts": 5,
    "reconnect_delay_seconds": 2
  }
}
```

Key design decisions:
- Azure keys are **never** stored in this file. The config only records the **names** of the environment variables to read. This keeps the config file safe to share or back up.
- `input_device_id` and `output_device_id` store the OS-level device identifier string (not an index, which can change). A `null` value means "use OS default".
- `secondary_output_device_id` enables the dual-output feature (e.g., physical speakers + virtual cable simultaneously).

#### 4.2.2 Audio Router (`audio_router.py`)

Responsibilities:
- Enumerate all audio input and output devices on the system, returning friendly names and stable identifiers.
- Detect virtual audio devices (VB-CABLE, VoiceMeeter, CABLE Input, etc.).
- Provide real-time audio level (RMS) from the selected input for the VU meter.
- Construct the correct `AudioConfig` / `AudioOutputConfig` objects for the Azure SDK based on the user's device selection.
- Handle dual-output by intercepting the TTS audio stream, duplicating it, and writing to two output devices.

**Device Enumeration Strategy:**

The Azure Speech SDK's `AudioConfig` supports specifying a device by its **endpoint ID** on Windows (WASAPI device ID). We will use `sounddevice` (which wraps PortAudio) for enumeration because it provides:
- Device name
- Host API (WASAPI, MME, etc.)
- Number of input/output channels
- Default sample rate

For virtual audio cables, we will look for known name patterns (`CABLE Input`, `VoiceMeeter`, `VB-Audio`, `Line`, etc.) and flag them in the UI with a distinct icon.

```
class AudioRouter:
    def list_input_devices() -> list[AudioDevice]
    def list_output_devices() -> list[AudioDevice]
    def get_input_level(device_id: str) -> float          # 0.0–1.0 RMS
    def build_input_config(device_id: str | None) -> AudioConfig
    def build_output_config(device_id: str | None) -> AudioOutputConfig
    def build_dual_output(primary_id, secondary_id) -> DualAudioOutput
```

`AudioDevice` is a dataclass:

```python
@dataclass
class AudioDevice:
    device_id: str          # Stable OS identifier
    name: str               # Friendly name ("Microphone (Midas M32)")
    host_api: str           # "WASAPI", "MME", etc.
    channels: int
    default_sample_rate: int
    is_virtual: bool        # Heuristic flag
    direction: str          # "input" | "output"
```

**Dual Output Implementation:**

Azure's `SpeechSynthesizer` can be configured to write to an `AudioOutputStream` (pull or push stream) instead of a device. For dual output:

1. Configure the synthesizer with a `PullAudioOutputStream`.
2. A dedicated thread reads from this stream.
3. The raw PCM data is written simultaneously to two `sounddevice.OutputStream` instances (one per target device).

This approach is more complex but gives us full control over where audio goes.

#### 4.2.3 Translation Pipeline (`translation_pipeline.py`)

Responsibilities:
- Own the lifecycle of the Azure `TranslationRecognizer`.
- Manage the TTS queue and the TTS worker thread.
- Apply SSML-based speed and pitch adjustments to every TTS utterance.
- Track timestamps for lag computation.
- Emit structured events that the GUI subscribes to.

**Pipeline Flow:**

```
 Mic audio ──► Azure STT/Translation ──► recognized event
                                              │
                                    ┌─────────▼──────────┐
                                    │   TTS Queue         │
                                    │  (thread-safe)      │
                                    └─────────┬──────────┘
                                              │
                                    ┌─────────▼──────────┐
                                    │   TTS Worker        │
                                    │  (SSML synthesis)   │
                                    └─────────┬──────────┘
                                              │
                                    ┌─────────▼──────────┐
                                    │   Audio Router      │
                                    │  (output device)    │
                                    └────────────────────┘
```

**SSML for Speed and Pitch Control:**

Instead of calling `speak_text_async(text)`, we construct SSML:

```xml
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
  <voice name="en-US-JennyNeural">
    <prosody rate="+20%" pitch="+5%">
      Hello, this is the translated text.
    </prosody>
  </voice>
</speak>
```

This allows real-time control of:
- `rate`: speaking speed, e.g. `"slow"`, `"fast"`, `"+30%"`, `"1.5"`
- `pitch`: voice pitch, e.g. `"low"`, `"high"`, `"+10%"`, `"-5%"`
- `volume`: output volume (though we prefer OS-level volume control)

The SSML is rebuilt for each utterance using the current config values, so the operator can adjust mid-session.

**Lag Tracking:**

Each utterance entering the TTS queue is timestamped:

```python
@dataclass
class Utterance:
    text: str
    recognized_at: float        # time.monotonic() when Azure returned the translation
    queued_at: float            # time.monotonic() when placed in queue
    synthesis_started_at: float # set by TTS worker
    synthesis_done_at: float    # set by TTS worker
```

From these timestamps we compute:
- **Queue depth**: number of items in `tts_queue` at any moment.
- **Current lag**: `synthesis_done_at - recognized_at` for the most recently completed utterance. This is the wall-clock delay the listener experiences.
- **Estimated lag**: for items still in the queue, we estimate based on average TTS duration.
- **Lag trend**: increasing / stable / decreasing (computed over a sliding window of 10 utterances).

When queue depth exceeds the configurable `tts_queue_warning_threshold`, the GUI shows a warning. When it exceeds `max_tts_queue_size`, the pipeline can optionally:
1. Drop the oldest queued utterance (lossy but keeps real-time).
2. Automatically increase TTS speaking rate by 10% per excess item (adaptive rate — user can enable/disable this).

#### 4.2.4 Monitoring & Metrics (`monitor.py`)

Responsibilities:
- Aggregate lag data and compute statistics.
- Detect prolonged lag and emit warnings.
- Track Azure service health (consecutive errors, reconnect attempts).
- Provide data for the GUI's status bar and lag indicator.

Exposed data (updated every 500 ms):

```python
@dataclass
class TranslationMetrics:
    current_lag_ms: int
    avg_lag_ms: int
    queue_depth: int
    total_utterances: int
    dropped_utterances: int
    azure_errors: int
    session_duration_s: float
    is_connected: bool
    last_error: str | None
```

#### 4.2.5 GUI Layer (`gui/`)

Technology: **PyQt6**

Rationale: PyQt6 provides native-feeling widgets on Windows, supports system tray, global hotkeys, and has a mature signal/slot mechanism that maps perfectly to our event-driven architecture. It also supports stylesheets for theming.

**Main Window Layout:**

```
┌─────────────────────────────────────────────────────────┐
│  BCBTranslate                               [_][□][X]   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─── Audio ──────────────────────────────────────────┐ │
│  │  Input:  [▼ Microphone (Midas M32 Ch1)       ]    │ │
│  │          [||||||||░░░░░░░░] VU                     │ │
│  │  Output: [▼ CABLE Input (VB-Audio Virtual)    ]    │ │
│  │  2nd Out:[▼ Speakers (Realtek)                ]    │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  ┌─── Segmentation ──────────────────────────────────┐  │
│  │  Silence: [  500 ▴▾] ms                           │  │
│  │  ☑ Auto-adjust segmentation timeout               │  │
│  │  Target min: [ 5.0 ▴▾] s  Target max: [15.0 ▴▾] s│  │
│  └────────────────────────────────────────────────────┘  │
│                                                         │
│  ┌─── Translation ────────────────────────────────────┐ │
│  │  From: [▼ Romanian (ro-RO)] To: [▼ English (en) ] │ │
│  │  Voice: [▼ en-US-JennyNeural               ]      │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  ┌─── Voice Tuning ──────────────────────────────────┐  │
│  │  Speed: [====●=====] 1.2×                         │  │
│  │  Pitch: [=======●==] +5%                          │  │
│  └────────────────────────────────────────────────────┘  │
│                                                         │
│  ┌─── Status ────────────────────────────────────────┐  │
│  │  Lag: 1.2s  ●●●○○  Queue: 2  Session: 00:34:12   │  │
│  │  [▶ START]                    [⚙ Settings]        │  │
│  └────────────────────────────────────────────────────┘  │
│                                                         │
│  ┌─── Live Log ──────────────────────────────────────┐  │
│  │  14:23:01  RO: Bună ziua, frați și surori.        │  │
│  │  14:23:01  EN: Good day, brothers and sisters.     │  │
│  │  14:23:05  RO: Astăzi vom citi din Psalmul 23.    │  │
│  │  14:23:06  EN: Today we will read from Psalm 23.  │  │
│  │                                                    │  │
│  └────────────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────────┤
│  ● Connected to Azure  │  Seg: 500 ms  │  Queue: 0     │
└─────────────────────────────────────────────────────────┘
```

**Settings Dialog (separate window):**

Accessed via the gear icon. Organized in tabs:

- **Azure** — Region, instructions for setting env vars, connection test button.
- **Audio** — Device refresh, sample rate, channels, noise suppression toggle.
- **Translation** — Language pair, profanity filter, continuous language detection.
- **Voice** — Voice browser (list all Azure Neural voices for the target language, with a preview/test button), rate, pitch.
- **Behavior** — Queue limits, drop policy, adaptive rate toggle, reconnect settings.
- **Logging** — Log level, file logging toggle, transcript saving.
- **Interface** — Theme (light/dark), always-on-top, hotkey configuration, start minimized.

Every change in the Settings dialog is **live-previewed** where possible (e.g., changing voice triggers a test utterance). Clicking "Save" writes to the config file. "Cancel" reverts in-memory changes.

**Lag Indicator Design:**

The lag indicator uses both color and a 5-dot scale:

| Lag | Dots | Color | Meaning |
|-----|------|-------|---------|
| < 1s | ●○○○○ | Green | Excellent — real-time |
| 1–2s | ●●○○○ | Green | Good — slight natural delay |
| 2–4s | ●●●○○ | Yellow | Noticeable — speaker is fast |
| 4–6s | ●●●●○ | Orange | Warning — consider increasing TTS speed |
| > 6s | ●●●●● | Red | Critical — TTS cannot keep up, utterances may be dropped |

The exact numeric lag (in seconds) is always displayed alongside the dots.

**System Tray:**

When minimized, the app lives in the system tray with:
- Colored icon reflecting lag status (green/yellow/red).
- Right-click menu: Start/Stop, Show, Settings, Exit.
- Tooltip showing current lag and queue depth.

#### 4.2.6 OTA Updater (`updater.py`)

Responsibilities:
- Check the GitHub Releases API for a newer version on application startup.
- Present a non-blocking prompt if an update is available, showing version, size, and release notes.
- Download the Inno Setup installer to a temp file with a progress bar dialog.
- Launch the installer in `/SILENT` mode and exit the application so the upgrade proceeds in-place.
- Respect the user's `auto_check_updates` preference; also expose a manual "Check for Updates Now" button in Settings.

**Update Flow:**

```
Application Start
       │
       ▼
  auto_check_updates && GITHUB_REPO set?
       │
    ┌──┴──┐
   No     Yes
    │      │
    │      ▼
    │  Background QThread: GET /repos/{owner}/{repo}/releases/latest
    │      │
    │      ▼
    │  Latest tag > APP_VERSION?
    │      │
    │   ┌──┴──┐
    │  No     Yes
    │   │      │
    │   │      ▼
    │   │  Emit update_available(UpdateInfo)
    │   │      │
    │   │      ▼
    │   │  QMessageBox: "Version X available — download?"
    │   │      │
    │   │   ┌──┴──┐
    │   │  No     Yes
    │   │   │      │
    │   │   │      ▼
    │   │   │  Download dialog with progress bar
    │   │   │      │
    │   │   │      ▼
    │   │   │  Launch installer: BCBTranslate_Setup_X.exe /SILENT /CLOSEAPPLICATIONS
    │   │   │      │
    │   │   │      ▼
    │   │   │  Application exits → installer upgrades in-place → relaunches
    │   │   │
    └───┴───┴──── Normal startup continues
```

Key design decisions:
- Uses only `urllib.request` (stdlib) — no extra dependency.
- The installer `.exe` asset is identified from GitHub Release assets by file name.
- The Inno Setup installer's `/SILENT` flag shows a progress bar but requires no user interaction. `/CLOSEAPPLICATIONS` tells Inno Setup to close the running BCBTranslate instance.
- `GITHUB_REPO` in `version.py` controls whether OTA checks are active. An empty string disables them entirely.
- The update check is non-blocking (runs in a `QThread`); the app is fully usable while the check runs.

#### 4.2.7 Azure SDK Wrapper (`azure_wrapper.py`)

Responsibilities:
- Encapsulate all Azure Speech SDK interactions.
- Handle authentication and connection lifecycle.
- Provide automatic reconnection with exponential backoff.
- Abstract over the differences between `AudioConfig(use_default_microphone=True)` and `AudioConfig(device_name=specific_id)`.
- Manage SSML generation.

```python
class AzureTranslationService:
    def __init__(self, config: AppConfig, audio_router: AudioRouter)
    def start_recognition() -> None
    def stop_recognition() -> None
    def synthesize_ssml(ssml: str) -> SynthesisResult
    def list_available_voices(locale: str) -> list[VoiceInfo]
    def test_connection() -> bool

    # Events (Qt signals or callback-based)
    on_translated: Signal(str, str, float)   # (source_text, translated_text, timestamp)
    on_recognizing: Signal(str)              # partial/interim result
    on_error: Signal(str)                    # error message
    on_connected: Signal()
    on_disconnected: Signal()
```

**Reconnection Logic:**

```
on error:
    attempt = 0
    while attempt < max_reconnect_attempts:
        wait(reconnect_delay * 2^attempt)     # exponential backoff
        try connect()
        if success: break
        attempt++
    if all attempts fail:
        emit fatal_error → GUI shows "Connection Lost" dialog
```

---

## 5. Project Structure

```
BCBTranslate/
├── main.py                        # Entry point
├── requirements.txt               # Dependencies with pinned versions
├── .env.example                   # Template for Azure credentials
├── README.md                      # User-facing quick start guide
│
├── core/
│   ├── __init__.py
│   ├── config_manager.py          # Configuration load/save/validate
│   ├── translation_pipeline.py    # Orchestrates STT → queue → TTS flow
│   ├── azure_wrapper.py           # Azure SDK abstraction
│   ├── audio_router.py            # Device enumeration, routing, dual output
│   ├── monitor.py                 # Lag tracking, metrics, health checks
│   ├── models.py                  # Dataclasses: AudioDevice, Utterance, Metrics, AppConfig
│   └── updater.py                 # OTA update checker, downloader, installer launcher
│
├── gui/
│   ├── __init__.py
│   ├── main_window.py             # Primary window with all panels
│   ├── settings_dialog.py         # Tabbed settings dialog
│   ├── widgets/
│   │   ├── __init__.py
│   │   ├── vu_meter.py            # Audio level meter widget
│   │   ├── lag_indicator.py       # Colored dot-based lag display
│   │   ├── device_selector.py     # Combo box with device refresh
│   │   ├── log_panel.py           # Scrollable, filterable log view
│   │   └── voice_browser.py       # Voice selection with preview
│   ├── resources/
│   │   ├── icons/                 # App icons, tray icons
│   │   └── styles/
│   │       ├── dark.qss           # Dark theme stylesheet
│   │       └── light.qss          # Light theme stylesheet
│   └── tray.py                    # System tray integration
│
├── utils/
│   ├── __init__.py
│   ├── ssml_builder.py            # SSML construction helper
│   ├── transcript_writer.py       # Timestamped log-to-file
│   └── hotkey_manager.py          # Global hotkey registration
│
└── tests/
    ├── __init__.py
    ├── test_config_manager.py
    ├── test_audio_router.py
    ├── test_translation_pipeline.py
    ├── test_ssml_builder.py
    └── test_monitor.py
```

---

## 6. Key Implementation Details

### 6.1 Audio Device Selection with Azure SDK

The Azure Speech SDK on Windows accepts a WASAPI device endpoint ID via:

```python
audio_config = speechsdk.audio.AudioConfig(device_name=endpoint_id)
```

For output:
```python
audio_output = speechsdk.audio.AudioOutputConfig(device_name=endpoint_id)
```

The `endpoint_id` is a string like `{0.0.0.00000000}.{GUID}` on Windows. We obtain it through `sounddevice` or through `comtypes` accessing the Windows MMDevice API directly. `sounddevice.query_devices()` returns PortAudio device indices and names, but for WASAPI endpoint IDs, we need to go lower-level.

**Strategy:**

1. Use `comtypes` and the Windows Core Audio API (`MMDeviceEnumerator`) to get WASAPI endpoint IDs and friendly names.
2. Fall back to `sounddevice` names if the COM approach fails.
3. Cache the device list; refresh when the user clicks "Refresh" or when a device change notification is received.

```python
import comtypes
from comtypes import GUID
from ctypes import POINTER, cast, pointer

CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
IID_IMMDeviceEnumerator  = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')

def enumerate_wasapi_devices(direction: str) -> list[AudioDevice]:
    """Enumerate WASAPI audio devices with endpoint IDs."""
    ...
```

### 6.2 Virtual Audio Cable Integration

Virtual audio cables (VB-CABLE, VoiceMeeter) appear as regular audio devices in Windows. They are identified by name patterns:

- `CABLE Input (VB-Audio Virtual Cable)` — virtual "speaker" (what you output to)
- `CABLE Output (VB-Audio Virtual Cable)` — virtual "mic" (what Teams reads from)
- `VoiceMeeter Input` / `VoiceMeeter Output`

The application will:
1. Detect these by name pattern and mark them with `is_virtual = True`.
2. In the UI, group virtual devices separately with a clear label.
3. Provide a brief in-app guide: "To use with Microsoft Teams, select a virtual cable as Output here, then select its matching Input as the microphone in Teams."

No special code is needed to interact with virtual cables — they behave identically to physical devices from the WASAPI perspective.

### 6.3 Dual Output (Simultaneous Physical + Virtual)

When the user selects a secondary output device, the TTS audio must go to both simultaneously. The Azure SDK only supports one output device per synthesizer.

**Implementation:**

1. Configure the `SpeechSynthesizer` with a `PushAudioOutputStream` callback.
2. In the callback, receive raw PCM chunks.
3. Write each chunk to two `sounddevice.RawOutputStream` instances — one per device.

```python
class DualAudioOutput:
    def __init__(self, primary_device_id: str, secondary_device_id: str,
                 sample_rate: int = 16000, channels: int = 1):
        self.stream_primary = sd.RawOutputStream(
            device=primary_device_id, samplerate=sample_rate,
            channels=channels, dtype='int16'
        )
        self.stream_secondary = sd.RawOutputStream(
            device=secondary_device_id, samplerate=sample_rate,
            channels=channels, dtype='int16'
        )

    def write(self, audio_buffer: bytes):
        self.stream_primary.write(np.frombuffer(audio_buffer, dtype=np.int16))
        self.stream_secondary.write(np.frombuffer(audio_buffer, dtype=np.int16))
```

Azure's push stream callback:

```python
def push_stream_callback(audio_buffer: memoryview) -> int:
    data = bytes(audio_buffer)
    dual_output.write(data)
    return len(data)

stream = speechsdk.audio.PushAudioOutputStream(
    stream_callback=push_stream_callback
)
audio_output_config = speechsdk.audio.AudioOutputConfig(stream=stream)
```

### 6.4 SSML Builder

A utility that constructs valid SSML from parameters:

```python
class SSMLBuilder:
    @staticmethod
    def build(text: str, voice: str, rate: float, pitch: str, volume: int = 100) -> str:
        rate_str = f"{rate:.1f}" if rate != 1.0 else "1.0"
        return (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="en-US">'
            f'<voice name="{voice}">'
            f'<prosody rate="{rate_str}" pitch="{pitch}" volume="{volume}">'
            f'{_escape_xml(text)}'
            f'</prosody></voice></speak>'
        )
```

The rate can be a float (1.0 = normal, 1.5 = 50% faster) or a percentage string. Azure supports both. We normalize to a float internally and format appropriately.

### 6.5 Adaptive Speaking Rate

When enabled, the system automatically increases TTS speed as the queue grows:

```
effective_rate = base_rate + (queue_depth - warning_threshold) * 0.1
effective_rate = clamp(effective_rate, 0.5, 2.5)
```

Example: base rate is 1.0×, warning threshold is 3, queue has 5 items → effective rate = 1.0 + (5-3) × 0.1 = 1.2×. This helps the output "catch up" organically without operator intervention.

When the queue drains back below the threshold, the rate returns to the configured base.

The adaptive rate is displayed in the GUI so the operator knows it is active:
`Speed: 1.0× (adaptive: 1.2×)`

### 6.6 Lag Measurement

Lag is measured from the moment Azure returns a recognized translation to the moment the corresponding TTS audio finishes playing:

```
lag = tts_done_timestamp - recognition_timestamp
```

This captures:
- Time spent waiting in the queue (the dominant factor).
- TTS synthesis time (Azure network round-trip for TTS).
- Audio playback duration.

We also compute:
- **Pipeline latency** (excluding playback): `tts_start_timestamp - recognition_timestamp` — how long the utterance waited before synthesis began.
- **Rolling average** over the last 10 utterances.

### 6.7 Graceful Error Handling & Reconnection

Azure Speech SDK errors we must handle:

| Error | Response |
|-------|----------|
| `CancellationReason.Error` with `AuthenticationFailure` | Show error, do not retry (credentials are wrong) |
| `CancellationReason.Error` with `ConnectionFailure` | Retry with backoff |
| `CancellationReason.Error` with `ServiceTimeout` | Retry immediately once, then backoff |
| `CancellationReason.Error` with `RuntimeError` | Log, attempt restart |
| `CancellationReason.EndOfStream` | Normal end, restart if still running |
| Network-level exceptions | Retry with backoff |

During reconnection, the GUI shows a pulsing "Reconnecting..." indicator. The TTS queue is preserved (not flushed) so that any utterances recognized just before disconnection are still synthesized after reconnecting.

### 6.8 Transcript Logging

When enabled, the application writes a structured transcript to a text file:

```
=== BCBTranslate Session — 2026-04-02 10:00:00 ===
Source: Romanian (ro-RO) → English (en)
Voice: en-US-JennyNeural | Rate: 1.0× | Pitch: +0%
========================================

[10:00:05] RO: Bună ziua tuturor.
[10:00:05] EN: Good day to all. (lag: 1.1s)

[10:00:12] RO: Astăzi vorbim despre credință.
[10:00:13] EN: Today we talk about faith. (lag: 1.3s)

...

=== Session ended — 10:45:00 | Duration: 00:45:00 | Utterances: 312 | Avg lag: 1.4s ===
```

File name format: `transcript_2026-04-02_10-00-00.txt`

---

## 7. Technology Stack

| Component | Technology | Version | Rationale |
|-----------|-----------|---------|-----------|
| Language | Python | 3.11+ | Existing prototype language; Azure SDK has excellent Python support |
| GUI Framework | PyQt6 | 6.6+ | Native feel, signal/slot pattern, stylesheet theming, system tray, good Windows support |
| Azure SDK | azure-cognitiveservices-speech | 1.38+ | Core translation and TTS engine |
| Audio enumeration | sounddevice + comtypes | latest | Cross-platform audio with WASAPI fallback for endpoint IDs |
| Audio processing | numpy | latest | PCM buffer manipulation for dual output |
| Config format | JSON | — | Human-readable, built-in Python support, no extra dependency |
| Env management | python-dotenv | latest | Load .env files for API keys |
| Global hotkeys | pynput | latest | Cross-platform global keyboard listener |
| Packaging | PyInstaller | latest | Single-file .exe distribution for non-technical operators |

**`requirements.txt`:**

```
PyQt6>=6.6
azure-cognitiveservices-speech>=1.38
sounddevice>=0.4
numpy>=1.26
python-dotenv>=1.0
pynput>=1.7
comtypes>=1.4
```

---

## 8. Threading Model

The application uses multiple threads. Careful design prevents races and deadlocks.

```
Main Thread (Qt Event Loop)
  ├── GUI rendering, user interaction
  ├── Timer: poll metrics every 500ms → update lag display, VU meter
  │
  ├── [Thread] Azure Recognition
  │     └── Managed by Azure SDK internally
  │     └── Callbacks fire on SDK's internal thread → must marshal to Qt via signals
  │
  ├── [Thread] TTS Worker
  │     └── Pulls from tts_queue
  │     └── Calls synthesizer.speak_ssml_async().get() (blocking in this thread)
  │     └── Updates utterance timestamps
  │     └── Emits signal when utterance completes
  │
  ├── [Thread] VU Meter Sampler (when enabled)
  │     └── Reads input audio level at 30 Hz
  │     └── Posts RMS value to GUI via signal
  │
  └── [Thread] Hotkey Listener
        └── Listens for global keyboard shortcuts
        └── Emits signal on hotkey press
```

**Thread Safety Rules:**
1. All GUI updates happen on the main thread. Worker threads emit Qt signals; the main thread's event loop dispatches them to slots.
2. The `tts_queue` is a `queue.Queue` (inherently thread-safe).
3. Configuration changes from the GUI are applied via a lock-protected method on each component.
4. Metrics are read-only from the GUI side; the monitor updates them atomically.

---

## 9. Error Scenarios & Recovery

| Scenario | Detection | Recovery |
|----------|-----------|----------|
| Azure credentials missing | Startup validation | Show setup wizard, cannot proceed without valid creds |
| Azure credentials invalid | `AuthenticationFailure` on first connect | Show error dialog with link to Azure portal |
| Network lost mid-session | `ConnectionFailure` event | Auto-reconnect with backoff; queue preserves pending utterances |
| Audio device disconnected | `sounddevice` exception or Azure input error | Pause recognition, notify user, offer device refresh |
| TTS queue overflow (speaker too fast) | `queue.qsize() > max_tts_queue_size` | Drop oldest, increase rate (if adaptive), warn operator |
| Azure service degraded (slow responses) | Lag exceeds 10s sustained | Warning in UI; suggest operator slow down or increase TTS rate |
| Virtual cable not installed | No virtual devices found | Show info dialog explaining how to install VB-CABLE |

---

## 10. Future Extensibility

The architecture is designed to accommodate future enhancements without structural changes:

| Future Feature | Extension Point |
|----------------|----------------|
| Additional translation engines (Google, DeepL) | New class implementing a `TranslationService` protocol; swap via config |
| Whisper-based local STT | New `RecognitionService` implementation; plug into the pipeline |
| Multiple simultaneous target languages | Pipeline supports multiple target languages; add a tab per language in the GUI |
| Cloud-hosted mode (translation as a service) | The pipeline is already decoupled from the GUI; wrap it in a FastAPI server |
| Mobile companion app | Expose metrics and controls via a lightweight WebSocket API |
| Speaker diarization | Azure SDK supports diarization; add speaker labels to the log and transcript |
| Custom vocabulary / glossary | Azure STT supports phrase lists; add a glossary editor in Settings |
| Audio recording (raw input) | Tap the input stream in AudioRouter and write to WAV |
| Automatic language detection | Azure supports auto-detect source language; toggle in config |

---

## 11. Deployment & Distribution

### Development Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # fill in Azure credentials
python main.py
```

### Production Distribution

Package with PyInstaller into a single-folder distribution (not single-file, to avoid slow startup from temp extraction):

```bash
pyinstaller --windowed --name BCBTranslate --icon gui/resources/icons/app.ico main.py
```

The resulting `dist/BCBTranslate/` folder can be zipped and shared. No Python installation required on the target machine.

Alternatively, create an installer with **Inno Setup** for a proper Windows install experience with Start Menu shortcuts and uninstaller.

### First-Run Experience

On first launch (no config file exists):
1. Show a welcome/setup dialog.
2. Guide the user through:
   - Verifying Azure credentials (test connection button).
   - Selecting input and output devices.
   - Choosing a voice and testing it.
3. Save the initial config.
4. Open the main window, ready to translate.

---

## 12. Configuration Lifecycle

```
Application Start
       │
       ▼
  Config file exists?
       │
    ┌──┴──┐
   No     Yes
    │      │
    ▼      ▼
 Create   Load & Validate
 defaults      │
    │     Schema version match?
    │      │
    │   ┌──┴──┐
    │  No     Yes
    │   │      │
    │   ▼      │
    │ Migrate  │
    │   │      │
    └───┴──────┘
         │
         ▼
  Config in memory
         │
    User edits via GUI
         │
         ▼
  Validate changes
         │
    ┌────┴────┐
  Invalid    Valid
    │          │
    ▼          ▼
  Show       Apply to running
  error      components + save to disk
```

The config schema includes a `version` field. When the app is updated and the schema changes, a migration function transforms the old config to the new format without losing user settings.

---

## 13. Testing Strategy

| Layer | Testing Approach |
|-------|-----------------|
| Config Manager | Unit tests: load, save, validate, migrate, defaults |
| SSML Builder | Unit tests: various rate/pitch combinations, XML escaping |
| Audio Router | Integration tests: enumerate devices (mocked for CI, real for manual testing) |
| Monitor / Metrics | Unit tests: lag calculations, threshold detection, rolling averages |
| Translation Pipeline | Integration tests with Azure (requires credentials; marked as slow/optional) |
| GUI | Manual testing checklist; automated smoke test with `pytest-qt` |

---

## 14. Open Questions & Decisions

| # | Question | Recommendation | Status |
|---|----------|---------------|--------|
| 1 | Should we support macOS now or later? | Later — current requirement is Windows only; architecture avoids hard locks but WASAPI enumeration is Windows-specific | **Decided: Windows first** |
| 2 | VB-CABLE or VoiceMeeter as the recommended virtual cable? | VB-CABLE is simpler (one cable, free). Document VoiceMeeter as an alternative for advanced users. | **Open** |
| 3 | Should the TTS queue drop old or new utterances on overflow? | Drop oldest — the audience benefits from hearing the most recent translation, not a backlog. | **Recommended** |
| 4 | Should we support real-time interim/partial translations in the log? | Yes, but display them in gray/italic and replace with final text. Azure's `recognizing` event provides these. | **Recommended** |
| 5 | Should the app auto-update? | Implemented: checks GitHub Releases API on startup, prompts user, downloads Inno Setup installer, runs `/SILENT` upgrade. Configurable via `auto_check_updates`. | **Implemented** |

---

## 15. Implementation Phases

### Phase 1 — Core Engine (MVP)

- Configuration manager with JSON persistence.
- Audio device enumeration and selection (input + output).
- Azure translation pipeline with TTS queue.
- SSML-based speed and pitch control.
- Basic lag tracking.
- Minimal PyQt6 window with device selectors, start/stop, and log panel.

### Phase 2 — Full GUI & Monitoring

- Complete GUI with all panels (VU meter, lag indicator, voice browser).
- Settings dialog with all tabs.
- System tray integration.
- Adaptive speaking rate.
- Transcript logging.

### Phase 3 — Advanced Features

- Dual audio output.
- Global hotkeys.
- Auto-reconnect with backoff.
- First-run setup wizard.
- Dark/light theme toggle.

### Phase 4 — Distribution & Polish

- PyInstaller packaging.
- Inno Setup installer.
- Edge case hardening.
- Performance profiling and optimization.
- User documentation.

---

## 16. Summary

BCBTranslate transforms two fragile prototype scripts into a robust, configurable, operator-friendly real-time translation application. The layered architecture separates audio routing, translation logic, configuration, and presentation, making each independently testable and replaceable. Full device control — including virtual audio cables for Teams integration — eliminates the need to manipulate Windows audio defaults. Lag monitoring and adaptive rate control keep the translation in sync with fast speakers. Persistent configuration means the operator sets it up once and simply presses Start on subsequent sessions.
