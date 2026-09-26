"""windows - Automated Windows QEMU VM management and guest command execution via QGA."""

from windows.audio import (
    MicrophoneContext,
    MicrophoneController,
    PlaybackHandle,
    VirtualAudioSink,
    analyze_wav_data,
    convert_audio_to_wav,
    create_sine_wav,
    is_audio_host_available,
)
from windows.cd import (
    CDController,
    CDDevice,
    create_cdrom_iso,
)
from windows.console import ConsoleController, ConsoleInfo, ScreenRecorder
from windows.disk import (
    DiskController,
    ImageFileController,
    ImagePath,
    MountedPartition,
    PartitionInfo,
)
from windows.executor import (
    CommandController,
    CommandResult,
    SystemCommandContext,
    UserCommandContext,
)
from windows.file import FileController, RemoteFile, RemotePath, SharedFolder
from windows.firewall import (
    FirewallAction,
    FirewallController,
    FirewallDirection,
    FirewallError,
    FirewallProfile,
    FirewallRule,
    NetworkCategory,
)
from windows.image import Image
from windows.iso import (
    ISO,
    WindowsVersion,
    clear_image_cache,
    clear_iso_cache,
    create_dummy_iso,
    get_default_cache_dir,
    get_image_cache_dir,
    list_cached_images,
    list_cached_isos,
    resolve_iso,
)
from windows.machine import Machine, MachineUserContext, PowerController
from windows.network import (
    NetworkController,
    NetworkInterface,
    NICModel,
    PacketCapture,
    VirtualSwitch,
)
from windows.processes import ProcessController, ProcessInfo
from windows.qga import QGAClient, QGAError
from windows.registry import RegistryController
from windows.services import ServiceController, ServiceInfo
from windows.smb import SMBError, SMBServerManager
from windows.snapshot import Snapshot, SnapshotController, SnapshotError, SnapshotInfo, SnapshotList
from windows.ttd import (
    Bookmark,
    GDBRemoteClient,
    TTDController,
    TTDError,
    TTDRecording,
    TTDReplaySession,
    TTDRestrictionError,
)
from windows.usb import (
    USBController,
    USBDevice,
    create_usb_disk,
    parse_size_to_bytes,
)

__all__ = [
    "ISO",
    "Bookmark",
    "CDController",
    "CDDevice",
    "CommandController",
    "CommandResult",
    "ConsoleController",
    "ConsoleInfo",
    "DiskController",
    "FileController",
    "FirewallAction",
    "FirewallController",
    "FirewallDirection",
    "FirewallError",
    "FirewallProfile",
    "FirewallRule",
    "GDBRemoteClient",
    "Image",
    "ImageFileController",
    "ImagePath",
    "Machine",
    "MachineUserContext",
    "MicrophoneContext",
    "MicrophoneController",
    "MountedPartition",
    "NICModel",
    "NetworkCategory",
    "NetworkController",
    "NetworkInterface",
    "PacketCapture",
    "PartitionInfo",
    "PlaybackHandle",
    "PowerController",
    "ProcessController",
    "ProcessInfo",
    "QGAClient",
    "QGAError",
    "RegistryController",
    "RemoteFile",
    "RemotePath",
    "SMBError",
    "SMBServerManager",
    "ScreenRecorder",
    "ServiceController",
    "ServiceInfo",
    "SharedFolder",
    "Snapshot",
    "SnapshotController",
    "SnapshotError",
    "SnapshotInfo",
    "SnapshotList",
    "SystemCommandContext",
    "TTDController",
    "TTDError",
    "TTDRecording",
    "TTDReplaySession",
    "TTDRestrictionError",
    "USBController",
    "USBDevice",
    "UserCommandContext",
    "VirtualAudioSink",
    "VirtualSwitch",
    "WindowsVersion",
    "analyze_wav_data",
    "clear_image_cache",
    "clear_iso_cache",
    "convert_audio_to_wav",
    "create_cdrom_iso",
    "create_dummy_iso",
    "create_sine_wav",
    "create_usb_disk",
    "get_default_cache_dir",
    "get_image_cache_dir",
    "is_audio_host_available",
    "list_cached_images",
    "list_cached_isos",
    "parse_size_to_bytes",
    "resolve_iso",
]
