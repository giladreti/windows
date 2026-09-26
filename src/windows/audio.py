"""Audio and virtual microphone emulation controller for Windows QEMU VMs."""

import logging
import math
import os
import shutil
import struct
import subprocess
import tempfile
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from windows.machine import Machine

logger = logging.getLogger(__name__)


def is_audio_host_available() -> bool:
    """Check if host has PulseAudio or PipeWire tools available for virtual audio sinks."""
    if shutil.which("pactl"):
        try:
            res = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2)
            if res.returncode == 0:
                return True
        except Exception:
            pass
    return False


def convert_audio_to_wav(
    input_path: str | Path,
    output_path: str | Path | None = None,
    sample_rate: int = 48000,
    channels: int = 2,
) -> Path:
    """Convert any audio file (MP3, WAV, OGG, FLAC, AAC, etc.) to 16-bit PCM WAV.

    If input is already a 16-bit PCM WAV with matching parameters, it is returned directly.
    """
    inp = Path(input_path).resolve()
    if not inp.exists():
        raise FileNotFoundError(f"Audio file not found: {inp}")

    # Check if already matching WAV
    if inp.suffix.lower() == ".wav" and output_path is None:
        try:
            with wave.open(str(inp), "rb") as wf:
                if wf.getnchannels() == channels and wf.getframerate() == sample_rate and wf.getsampwidth() == 2:
                    return inp
        except Exception:
            pass

    if output_path is None:
        fd, tmp_file = tempfile.mkstemp(prefix="audio_conv_", suffix=".wav")
        os.close(fd)
        out = Path(tmp_file).resolve()
    else:
        out = Path(output_path).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(inp),
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(out),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg audio conversion failed for '{inp}': {res.stderr}")
        return out

    # If ffmpeg is missing and input is wav, try standard wave module
    if inp.suffix.lower() == ".wav":
        try:
            with wave.open(str(inp), "rb") as r_wf:
                with wave.open(str(out), "wb") as w_wf:
                    w_wf.setnchannels(r_wf.getnchannels())
                    w_wf.setsampwidth(r_wf.getsampwidth())
                    w_wf.setframerate(r_wf.getframerate())
                    w_wf.writeframes(r_wf.readframes(r_wf.getnframes()))
            return out
        except Exception as e:
            raise RuntimeError(f"Failed to copy WAV file without ffmpeg: {e}") from e

    raise RuntimeError(f"ffmpeg is required to convert '{inp.suffix}' files to WAV. Please install ffmpeg on the host.")


def create_sine_wav(
    output_path: str | Path,
    duration_sec: float = 2.0,
    frequency: float = 440.0,
    sample_rate: int = 48000,
) -> Path:
    """Generate a simple test sine-wave WAV file."""
    out = Path(output_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    num_samples = int(duration_sec * sample_rate)

    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw_data = bytearray()
        for i in range(num_samples):
            val = int(32767.0 * 0.5 * math.sin(2.0 * math.pi * frequency * i / sample_rate))
            raw_data.extend(struct.pack("<hh", val, val))
        wf.writeframes(raw_data)
    return out


class VirtualAudioSink:
    """Manages a PulseAudio/PipeWire null-sink module on the host to route audio into QEMU."""

    def __init__(self, sink_name: str | None = None):
        self.sink_name = sink_name or f"qemu_mic_{uuid.uuid4().hex[:8]}"
        self.source_name = f"{self.sink_name}.monitor"
        self._module_id: str | None = None
        self._playback_proc: subprocess.Popen | None = None
        self._converted_wav: Path | None = None

    def start_sink(self) -> None:
        """Load PulseAudio null-sink module."""
        if self._module_id is not None:
            return
        if not shutil.which("pactl"):
            logger.warning("pactl not found; audio sink emulation will operate in mock mode.")
            return

        cmd = [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={self.sink_name}",
            f"sink_properties=device.description=QEMU_Virtual_Microphone_{self.sink_name}",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self._module_id = res.stdout.strip()
        except Exception as e:
            logger.warning("Failed to load PulseAudio null-sink module: %s", e)

    def play(self, audio_file: str | Path, loop: bool = False) -> subprocess.Popen | None:
        """Play WAV/MP3 audio file into the virtual audio sink."""
        self.stop()
        self.start_sink()

        wav_path = convert_audio_to_wav(audio_file)
        if wav_path != Path(audio_file).resolve():
            self._converted_wav = wav_path

        # Determine playback tool
        paplay = shutil.which("paplay")
        pw_play = shutil.which("pw-play")
        ffmpeg = shutil.which("ffmpeg")

        if loop and ffmpeg:
            cmd = [
                ffmpeg,
                "-re",
                "-stream_loop",
                "-1",
                "-i",
                str(wav_path),
                "-f",
                "pulse",
                self.sink_name,
            ]
            self._playback_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif paplay:
            if loop:
                # Loop shell script
                sh_cmd = f"while true; do paplay -d {self.sink_name} '{wav_path}' || break; done"
                self._playback_proc = subprocess.Popen(
                    ["bash", "-c", sh_cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            else:
                self._playback_proc = subprocess.Popen(
                    [paplay, "-d", self.sink_name, str(wav_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        elif pw_play:
            cmd = [pw_play, f"--target={self.sink_name}", str(wav_path)]
            self._playback_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif ffmpeg:
            cmd = [ffmpeg, "-re", "-i", str(wav_path), "-f", "pulse", self.sink_name]
            self._playback_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            logger.warning("No audio player (paplay, pw-play, ffmpeg) found to stream audio.")
            return None

        return self._playback_proc

    def stop(self) -> None:
        """Stop current audio playback."""
        if self._playback_proc and self._playback_proc.poll() is None:
            self._playback_proc.terminate()
            try:
                self._playback_proc.wait(timeout=2.0)
            except Exception:
                self._playback_proc.kill()
        self._playback_proc = None

        if self._converted_wav and self._converted_wav.exists():
            try:
                self._converted_wav.unlink()
            except OSError:
                pass
            self._converted_wav = None

    def close(self) -> None:
        """Stop playback and unload PulseAudio null-sink module."""
        self.stop()
        if self._module_id and shutil.which("pactl"):
            try:
                subprocess.run(["pactl", "unload-module", self._module_id], capture_output=True, timeout=2.0)
            except Exception:
                pass
            self._module_id = None

    @property
    def is_playing(self) -> bool:
        """Whether audio playback process is actively running."""
        return self._playback_proc is not None and self._playback_proc.poll() is None


@dataclass
class PlaybackHandle:
    """Handle representing an active microphone playback session."""

    controller: "MicrophoneController"
    audio_path: Path
    loop: bool = False

    def stop(self) -> None:
        """Stop microphone playback."""
        self.controller.stop()

    @property
    def is_playing(self) -> bool:
        """Whether audio is actively playing into the microphone."""
        return self.controller.is_playing

    def __enter__(self) -> "PlaybackHandle":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


class MicrophoneController:
    """Controller for emulating guest microphone input from host audio files (MP3/WAV)."""

    def __init__(self, machine: "Machine"):
        self.machine = machine
        self._sink: VirtualAudioSink | None = None
        self._dev_id: str | None = None
        self._current_audio: Path | None = None
        self._is_attached = False

    @property
    def is_playing(self) -> bool:
        """Check if audio is currently playing into the microphone."""
        return self._sink is not None and self._sink.is_playing

    @property
    def is_attached(self) -> bool:
        """Check if virtual microphone device is attached to the guest."""
        return self._is_attached

    @property
    def sink(self) -> VirtualAudioSink:
        """Get or initialize host VirtualAudioSink."""
        if self._sink is None:
            self._sink = VirtualAudioSink()
            self._sink.start_sink()
        return self._sink

    def attach(self, audio_file: str | Path | None = None, loop: bool = False) -> None:
        """Attach an emulated USB microphone device into the guest virtual machine.

        Args:
            audio_file: Optional MP3 or WAV file to immediately play into the microphone.
            loop: If True, continuously loops audio playback.
        """
        if not self.machine.is_running:
            raise RuntimeError("Machine must be running to attach microphone device.")

        # Ensure sink is created
        _ = self.sink

        if not self._is_attached:
            if hasattr(self.machine, "console") and self.machine.console:
                self._dev_id = f"usb_mic_{uuid.uuid4().hex[:8]}"
                # Hotplug USB audio device
                cmd = f"device_add usb-audio,id={self._dev_id},audiodev=snd0"
                out = self.machine.console.send_monitor_command(cmd)
                out_lower = out.lower()
                if "error" in out_lower or "failed" in out_lower or "can't" in out_lower:
                    logger.warning("Failed to hotplug usb-audio device: %s", out.strip())
            self._is_attached = True

        if audio_file:
            self.play(audio_file, loop=loop)

    def detach(self) -> None:
        """Detach the emulated microphone device from the guest virtual machine."""
        self.stop()
        if self._dev_id and hasattr(self.machine, "console") and self.machine.console:
            try:
                self.machine.console.send_monitor_command(f"device_del {self._dev_id}")
            except Exception:
                pass
        self._dev_id = None
        self._is_attached = False

        if self._sink:
            self._sink.close()
            self._sink = None

    def play(self, audio_file: str | Path, loop: bool = False) -> PlaybackHandle:
        """Play an MP3 or WAV audio file into the guest microphone input.

        Args:
            audio_file: Path to host MP3, WAV, or other audio file.
            loop: If True, loops audio continuously.

        Returns:
            PlaybackHandle controlling the playback session.
        """
        p = Path(audio_file).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Audio file not found: {p}")

        if not self._is_attached and self.machine.is_running:
            self.attach()

        self._current_audio = p
        self.sink.play(p, loop=loop)
        return PlaybackHandle(controller=self, audio_path=p, loop=loop)

    def record_guest(
        self,
        duration_sec: float = 3.0,
        output_path: str | Path | None = None,
    ) -> bytes:
        """Record audio from the virtual microphone inside the Windows guest and return the WAV bytes.

        Uses the guest's native WinMM audio subsystem to capture live PCM audio from the default
        recording input into a WAV file.

        Args:
            duration_sec: Number of seconds to record (default 3.0).
            output_path: Optional destination path inside the guest (e.g. 'C:\\recorded.wav').
                         If None, a temporary guest path is used and cleaned up.

        Returns:
            Raw bytes of the recorded WAV file.
        """
        if not self.machine.is_running:
            raise RuntimeError("Machine must be running to record audio in guest.")
        if not hasattr(self.machine, "command") or not self.machine.command:
            raise RuntimeError("Machine command controller is required to record audio in guest.")

        guest_wav = str(output_path) if output_path else f"C:\\mic_rec_{uuid.uuid4().hex[:8]}.wav"
        sleep_sec = max(0.5, float(duration_sec))

        ps_script = f"""
$code = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class AudioRecorder {{
    [DllImport("winmm.dll", EntryPoint = "mciSendStringA", CharSet = CharSet.Ansi)]
    public static extern int mciSendString(string command, StringBuilder buffer, int bufferSize, IntPtr hwndCallback);

    public static void StartRecording() {{
        mciSendString("open new type waveaudio alias recsound", null, 0, IntPtr.Zero);
        mciSendString("set recsound bitspersample 16 channels 1 samplespersec 44100", null, 0, IntPtr.Zero);
        mciSendString("record recsound", null, 0, IntPtr.Zero);
    }}

    public static void StopAndSave(string path) {{
        mciSendString("stop recsound", null, 0, IntPtr.Zero);
        mciSendString("save recsound " + path, null, 0, IntPtr.Zero);
        mciSendString("close recsound", null, 0, IntPtr.Zero);
    }}
}}
"@
Add-Type -TypeDefinition $code -Language CSharp
[AudioRecorder]::StartRecording()
Start-Sleep -Seconds {sleep_sec}
[AudioRecorder]::StopAndSave("{guest_wav}")
Test-Path "{guest_wav}"
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, timeout=int(sleep_sec) + 30)
        if res.stdout.strip().lower() != "true":
            raise RuntimeError(
                f"Failed to record audio inside Windows guest: {res.stdout.strip()} {res.stderr.strip()}"
            )

        data = self.machine.file.path(guest_wav).read_bytes()

        if output_path is None:
            try:
                self.machine.command.run(
                    f"Remove-Item -Force '{guest_wav}' -ErrorAction SilentlyContinue",
                    powershell=True,
                    auto_retry=False,
                )
            except Exception:
                pass

        return data

    def stop(self) -> None:
        """Stop current microphone audio playback."""
        if self._sink:
            self._sink.stop()
        self._current_audio = None

    def emulate(self, audio_file: str | Path, loop: bool = False) -> "MicrophoneContext":
        """Context manager that attaches the microphone, plays audio, and detaches on exit."""
        return MicrophoneContext(self, audio_file, loop=loop)

    def close(self) -> None:
        """Clean up all playback and virtual sinks."""
        self.detach()


def analyze_wav_data(wav_bytes: bytes) -> dict[str, Any]:
    """Inspect and analyze raw WAV audio bytes, returning format metadata and signal statistics."""
    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        nframes = wf.getnframes()
        frames = wf.readframes(nframes)

    if sampwidth == 1:
        raw_samples = struct.unpack(f"{nframes * channels}B", frames)
        amplitudes = [abs(s - 128) for s in raw_samples]
    elif sampwidth == 2:
        raw_samples = struct.unpack(f"<{nframes * channels}h", frames)
        amplitudes = [abs(s) for s in raw_samples]
    elif sampwidth == 4:
        raw_samples = struct.unpack(f"<{nframes * channels}i", frames)
        amplitudes = [abs(s) for s in raw_samples]
    else:
        amplitudes = [0]

    max_amp = max(amplitudes) if amplitudes else 0
    avg_amp = (sum(amplitudes) / len(amplitudes)) if amplitudes else 0.0

    return {
        "channels": channels,
        "sampwidth": sampwidth,
        "framerate": framerate,
        "nframes": nframes,
        "duration_sec": nframes / framerate if framerate > 0 else 0.0,
        "max_amplitude": max_amp,
        "avg_amplitude": avg_amp,
        "is_silent": max_amp <= 5,
    }


class MicrophoneContext:
    """Context manager for scoped microphone emulation."""

    def __init__(self, controller: MicrophoneController, audio_file: str | Path, loop: bool = False):
        self.controller = controller
        self.audio_file = Path(audio_file).resolve()
        self.loop = loop
        self.playback: PlaybackHandle | None = None

    @property
    def is_attached(self) -> bool:
        """Check if virtual microphone device is attached to the guest."""
        return self.controller.is_attached

    @property
    def is_playing(self) -> bool:
        """Check if audio is currently playing into the virtual microphone."""
        return self.controller.is_playing

    def record(self, duration_sec: float = 3.0, output_path: str | Path | None = None) -> bytes:
        """Record live audio from within the guest during this active microphone emulation session."""
        return self.controller.record_guest(duration_sec=duration_sec, output_path=output_path)

    def analyze(self, duration_sec: float = 3.0) -> dict[str, Any]:
        """Record and analyze live guest audio to verify that sound was captured."""
        data = self.record(duration_sec=duration_sec)
        return analyze_wav_data(data)

    def __enter__(self) -> "MicrophoneContext":
        self.controller.attach(self.audio_file, loop=self.loop)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.controller.detach()
