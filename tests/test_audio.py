"""Unit tests for virtual microphone emulation, audio conversion, and MicrophoneController."""

import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from windows.audio import (
    MicrophoneController,
    PlaybackHandle,
    VirtualAudioSink,
    convert_audio_to_wav,
    create_sine_wav,
)


def test_create_sine_wav(tmp_path):
    wav_path = tmp_path / "sine.wav"
    res = create_sine_wav(wav_path, duration_sec=0.5, frequency=440.0, sample_rate=48000)
    assert res == wav_path
    assert wav_path.exists()
    assert wav_path.stat().st_size > 0

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 48000
        assert wf.getnframes() == int(0.5 * 48000)


def test_convert_audio_to_wav(tmp_path):
    orig_wav = tmp_path / "orig.wav"
    create_sine_wav(orig_wav, duration_sec=0.2)

    # Calling with output_path=None on matching wav returns it directly
    direct = convert_audio_to_wav(orig_wav)
    assert direct == orig_wav

    # Convert to explicit destination
    dest_wav = tmp_path / "converted.wav"
    res = convert_audio_to_wav(orig_wav, output_path=dest_wav)
    assert res == dest_wav
    assert dest_wav.exists()


def test_convert_audio_file_not_found():
    with pytest.raises(FileNotFoundError):
        convert_audio_to_wav("/path/that/does/not/exist.wav")


def test_virtual_audio_sink_lifecycle():
    with (
        patch("shutil.which") as mock_which,
        patch("subprocess.run") as mock_run,
        patch("subprocess.Popen") as mock_popen,
    ):
        mock_which.side_effect = lambda cmd: "/usr/bin/" + cmd if cmd in ("pactl", "paplay") else None
        mock_run.return_value.stdout = "12345"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        sink = VirtualAudioSink(sink_name="test_sink")
        sink.start_sink()
        assert sink._module_id == "12345"
        assert sink.source_name == "test_sink.monitor"

        # Test play
        with patch("windows.audio.convert_audio_to_wav", return_value=Path("/tmp/fake.wav")):
            p = sink.play("/tmp/fake.wav", loop=False)
            assert p == mock_proc
            assert sink.is_playing is True

        # Test stop
        sink.stop()
        mock_proc.terminate.assert_called()
        assert sink.is_playing is False

        # Test close
        sink.close()
        assert sink._module_id is None


def test_microphone_controller_methods(tmp_path):
    wav_file = tmp_path / "voice.wav"
    create_sine_wav(wav_file, duration_sec=0.1)

    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.console.send_monitor_command.return_value = "OK"

    ctrl = MicrophoneController(mock_machine)
    with patch.object(ctrl.sink, "play") as mock_sink_play, patch.object(ctrl.sink, "stop") as mock_sink_stop:
        # Attach
        ctrl.attach()
        assert ctrl.is_attached is True
        calls = [c[0][0] for c in mock_machine.console.send_monitor_command.call_args_list]
        assert any("device_add usb-audio" in c for c in calls)

        # Play
        handle = ctrl.play(wav_file)
        assert isinstance(handle, PlaybackHandle)
        mock_sink_play.assert_called()

        # Stop
        ctrl.stop()
        mock_sink_stop.assert_called()

        # Detach
        ctrl.detach()
        assert ctrl.is_attached is False
        detach_calls = [c[0][0] for c in mock_machine.console.send_monitor_command.call_args_list]
        assert any("device_del" in c for c in detach_calls)


def test_microphone_context_manager(tmp_path):
    wav_file = tmp_path / "voice2.wav"
    create_sine_wav(wav_file, duration_sec=0.1)

    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.console.send_monitor_command.return_value = "OK"

    ctrl = MicrophoneController(mock_machine)
    with patch.object(ctrl.sink, "play"), patch.object(ctrl.sink, "stop"), patch.object(ctrl.sink, "close"):
        with ctrl.emulate(wav_file) as mic:
            assert mic.controller == ctrl
            assert mic.is_attached is True

        assert ctrl.is_attached is False


def test_microphone_not_running_error():
    mock_machine = MagicMock()
    mock_machine.is_running = False

    ctrl = MicrophoneController(mock_machine)
    with pytest.raises(RuntimeError, match="Machine must be running"):
        ctrl.attach()


def test_record_guest_and_analyze(tmp_path):
    from windows.audio import analyze_wav_data

    # Generate a sine wave to test analysis
    sine_path = tmp_path / "test_sine.wav"
    create_sine_wav(sine_path, duration_sec=0.2, frequency=440.0)
    data = sine_path.read_bytes()

    analysis = analyze_wav_data(data)
    assert analysis["channels"] in (1, 2)
    assert analysis["sampwidth"] == 2
    assert analysis["framerate"] in (44100, 48000)
    assert analysis["duration_sec"] > 0.15
    assert analysis["max_amplitude"] > 1000
    assert analysis["is_silent"] is False

    # Test record_guest with mock machine
    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.command.run.return_value.stdout = "True\n"
    mock_machine.file.path.return_value.read_bytes.return_value = data

    ctrl = MicrophoneController(mock_machine)
    rec = ctrl.record_guest(duration_sec=1.0)
    assert rec == data
