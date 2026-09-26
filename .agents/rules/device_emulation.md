# Hardware & Device Emulation Guidelines

Guidelines for implementing and maintaining hardware emulation in `windows`:

## 1. USB Storage Emulation (`src/windows/usb.py`)
- **Partitioning**: When formatting raw disk images on the host, always write an MBR partition table with sector 2048 start offset for FAT32 (`mkfs.vfat --offset=2048`). Raw unpartitioned disks are treated as superfloppies by Windows and may fail to mount properly.
- **Non-Destructive Detection**: Never run destructive partition initialization (`Initialize-Disk`, `Format-Volume`) in the guest detection script, as Windows may take a few seconds to discover partitions upon device hotplug.
- **Cleanup**: Eject via QEMU monitor `device_del <id>` followed by `drive_del <id>`.

## 2. CD-ROM & ISO Media Emulation (`src/windows/cd.py`)
- **ISO Format**: Always generate ISO 9660 with Joliet Level 3 (`joliet=3`) via `pycdlib` to support full Unicode long filenames and nested directory structures.
- **Onboard IDE/SATA vs Hotplug**:
  - Prefer onboard IDE/SATA drive (`change cd0 <path>`) when available.
  - Fall back to hotplugged USB CD-ROM (`usb-storage,removable=true,media=cdrom`) when multiple drives or dynamic devices are needed.
- **Eject**: Issue `eject -f <drive_id>` over the monitor to signal media removal to the guest OS.

## 3. Microphone Audio Emulation (`src/windows/audio.py`)
- **Host Audio Isolation**: Always use PulseAudio/PipeWire virtual null sinks (`module-null-sink`) so host system audio is not polluted or played through physical speakers.
- **QEMU Audio Binding**: Bind QEMU's `-audiodev` input to the virtual sink's monitor (`<sink_name>.monitor`).
- **In-Guest Recording**: Use native Windows MCI (`mciSendString`) or Windows Audio Services to record from the default recording device.
- **Signal Verification**: Inspect PCM sample amplitudes using `analyze_wav_data()` to ensure non-silence (`max_amplitude > 10`).
