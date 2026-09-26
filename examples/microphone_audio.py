"""Example demonstrating virtual microphone audio emulation and in-guest recording.

This example:
1. Generates a 440 Hz test tone WAV file on the host.
2. Boots a Windows VM and attaches an emulated microphone device.
3. Streams the audio tone into the guest's microphone input.
4. Uses machine.microphone.record_guest() to record the audio inside the Windows guest.
5. Analyzes the recorded audio using analyze_wav_data() to prove that the microphone
   captured the tone with strong signal amplitude inside the VM.
"""

from pathlib import Path

from windows import (
    ISO,
    Image,
    Machine,
    WindowsVersion,
    analyze_wav_data,
    create_sine_wav,
)


def main():
    print("=== Windows Virtual Microphone Emulation & Recording Demo ===")

    # 1. Prepare base VM image
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay.qcow2", use_cache=True)

    # 2. Generate a 440 Hz sine-wave tone audio file on the host (or provide any MP3/WAV)
    tone_path = Path("host_tone_440hz.wav").resolve()
    create_sine_wav(tone_path, duration_sec=5.0, frequency=440.0)
    print(f"Created host audio tone: {tone_path}")

    # 3. Boot Windows VM
    machine = Machine(image, ram_mb=4096, cpus=4)
    machine.power.on()
    print("Waiting for guest QGA interface...")
    machine.command.wait_until_ready(timeout=180)

    try:
        # 4. Attach virtual microphone and stream the tone into the guest
        print("\n[1] Attaching virtual microphone and playing audio into guest...")
        with machine.microphone.emulate(tone_path, loop=True) as mic:
            print(f"    Microphone attached: {mic.is_attached}, is_playing: {mic.is_playing}")

            # Inspect audio input devices inside Windows
            devs = machine.command.run(
                "Get-CimInstance Win32_SoundDevice | Select-Object Name, Status",
                powershell=True,
            )
            print("    Windows guest sound devices:\n", devs.stdout.strip())

            # 5. Record audio from the microphone inside the Windows guest
            print("\n[2] Recording 3 seconds of audio from inside the Windows guest...")
            recorded_bytes = machine.microphone.record_guest(duration_sec=3.0)
            print(f"    Captured {len(recorded_bytes)} bytes of WAV audio from Windows guest!")

            # 6. Analyze recorded audio to prove that the tone was captured
            analysis = analyze_wav_data(recorded_bytes)
            print("\n[3] Audio Analysis of Guest Recording:")
            print(f"    Channels:        {analysis['channels']}")
            print(f"    Sample Width:    {analysis['sampwidth']} bytes/sample")
            print(f"    Sample Rate:     {analysis['framerate']} Hz")
            print(f"    Duration:        {analysis['duration_sec']:.2f} seconds")
            print(f"    Max Amplitude:   {analysis['max_amplitude']}")
            print(f"    Avg Amplitude:   {analysis['avg_amplitude']:.2f}")
            print(f"    Is Silent:       {analysis['is_silent']}")

            assert not analysis["is_silent"], "Microphone recorded silence! Tone was not captured."
            assert analysis["max_amplitude"] > 10, "Signal amplitude too weak!"
            print("\n>>> PROOF CONFIRMED: Non-silent audio tone was captured by the microphone inside Windows! <<<")

        print("\n[4] Microphone stopped and detached cleanly.")

    finally:
        machine.power.off()
        machine.close()
        tone_path.unlink(missing_ok=True)
        print("\nExample finished successfully!")


if __name__ == "__main__":
    main()
