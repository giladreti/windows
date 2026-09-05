"""windows - Automated Windows QEMU VM management and guest command execution via QGA."""

from windows.console import ConsoleController, ConsoleInfo
from windows.executor import CommandController, CommandResult
from windows.file import FileController, RemoteFile, RemotePath
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
from windows.machine import Machine, PowerController
from windows.processes import ProcessController, ProcessInfo
from windows.qga import QGAClient, QGAError
from windows.registry import RegistryController
from windows.services import ServiceController, ServiceInfo

__all__ = [
    "ISO",
    "CommandController",
    "CommandResult",
    "ConsoleController",
    "ConsoleInfo",
    "FileController",
    "Image",
    "Machine",
    "PowerController",
    "ProcessController",
    "ProcessInfo",
    "QGAClient",
    "QGAError",
    "RegistryController",
    "RemoteFile",
    "RemotePath",
    "ServiceController",
    "ServiceInfo",
    "WindowsVersion",
    "clear_image_cache",
    "clear_iso_cache",
    "create_dummy_iso",
    "get_default_cache_dir",
    "get_image_cache_dir",
    "list_cached_images",
    "list_cached_isos",
    "resolve_iso",
]
