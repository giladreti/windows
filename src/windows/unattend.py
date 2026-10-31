"""Unattended XML (autounattend.xml) generator and ISO builder for automated Windows installation."""

import io
import shutil
from pathlib import Path

import pycdlib

DEFAULT_USERNAME = "Administrator"
DEFAULT_PASSWORD = "Password123!"
# Windows XP GUI setup refuses reserved account names ("Administrator", "Guest")
# as the registered owner name, so fall back to a innocuous default instead.
DEFAULT_XP_FULL_NAME = "Windows XP User"
DEFAULT_SETUP_KEY = (
    "W269N-WFGWX-YVC9B-4J6C9-T83GX"  # Official Microsoft Generic KMS setup key for Windows 10/11 Pro Evaluation
)

SETUP_GUEST_CMD = """@echo off
echo === Starting Guest Tools & QGA Setup === > C:\\windows_setup.log 2>&1
reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f >> C:\\windows_setup.log 2>&1
powershell -ExecutionPolicy Bypass -Command "Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue" >> C:\\windows_setup.log 2>&1
powershell -ExecutionPolicy Bypass -Command "Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled False" >> C:\\windows_setup.log 2>&1
netsh advfirewall set allprofiles state off >> C:\\windows_setup.log 2>&1
net user Administrator Password123! /active:yes >> C:\\windows_setup.log 2>&1

echo === Installing VirtIO Drivers === >> C:\\windows_setup.log 2>&1
powershell -ExecutionPolicy Bypass -Command "Get-ChildItem -Path C:\\,D:\\,E:\\,F:\\,G:\\ -Filter virtio-win-guest-tools.exe -Recurse -ErrorAction SilentlyContinue | ForEach-Object { Start-Process $_.FullName -ArgumentList '/passive', '/norestart' -Wait }" >> C:\\windows_setup.log 2>&1

echo === Installing QEMU Guest Agent === >> C:\\windows_setup.log 2>&1
powershell -ExecutionPolicy Bypass -Command "Get-ChildItem -Path C:\\,D:\\,E:\\,F:\\,G:\\ -Filter qemu-ga-x86_64.msi -Recurse -ErrorAction SilentlyContinue | ForEach-Object { Start-Process msiexec.exe -ArgumentList '/i', $_.FullName, '/qn', '/norestart' -Wait }" >> C:\\windows_setup.log 2>&1
sc config QEMU-GA start= auto >> C:\\windows_setup.log 2>&1
net start QEMU-GA >> C:\\windows_setup.log 2>&1
echo === Configuring Kernel Debugging === >> C:\\windows_setup.log 2>&1
bcdedit /debug on >> C:\\windows_setup.log 2>&1
bcdedit /set testsigning on >> C:\\windows_setup.log 2>&1
bcdedit /dbgsettings serial debugport:1 baudrate:115200 >> C:\\windows_setup.log 2>&1

echo === Configuring Boot and Recovery Policies === >> C:\\windows_setup.log 2>&1
bcdedit /set {default} bootstatuspolicy ignoreallfailures >> C:\\windows_setup.log 2>&1
bcdedit /set {default} recoveryenabled no >> C:\\windows_setup.log 2>&1
bcdedit /set {current} bootstatuspolicy ignoreallfailures >> C:\\windows_setup.log 2>&1
bcdedit /set {current} recoveryenabled no >> C:\\windows_setup.log 2>&1

powercfg /h off >> C:\\windows_setup.log 2>&1
powercfg /change standby-timeout-ac 0 >> C:\\windows_setup.log 2>&1
powercfg /change monitor-timeout-ac 0 >> C:\\windows_setup.log 2>&1
powercfg /setacvalueindex scheme_current sub_pci express 0 >> C:\\windows_setup.log 2>&1
powercfg /setactive scheme_current >> C:\\windows_setup.log 2>&1
sc config intelppm start= disabled >> C:\\windows_setup.log 2>&1
reg add "HKLM\\SYSTEM\\CurrentControlSet\\Services\\intelppm" /v Start /t REG_DWORD /d 4 /f >> C:\\windows_setup.log 2>&1

:: Register post-reboot finalizer to wait for TiWorker, TrustedInstaller, and updates to finish before shutdown
reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\RunOnce" /v FinalizeInstall /t REG_SZ /d "powershell.exe -ExecutionPolicy Bypass -NoProfile -Command \"$max=300;$w=0;while($w -lt $max){$p=Get-Process TiWorker,TrustedInstaller,msiexec -ErrorAction SilentlyContinue;if(-not $p){break};Start-Sleep 5;$w+=5};shutdown /s /t 10 /f\"" /f >> C:\\windows_setup.log 2>&1

echo === Setup Complete === >> C:\\windows_setup.log 2>&1
"""


def generate_unattend_xml(
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    computer_name: str = "WINDOWS-WINVM",
    language: str = "en-US",
    input_locale: str = "0409:00000409",
    time_zone: str = "UTC",
    image_name: str = "Windows 10 Pro",
    bypass_tpm: bool = True,
) -> str:
    """Generate autounattend.xml for 100% automated, hands-free Windows installation."""
    local_accounts_xml = ""
    if username.lower() != "administrator":
        local_accounts_xml = f"""
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{username}</Name>
            <Group>Administrators</Group>
            <Password>
              <Value>{password}</Value>
              <PlainText>true</PlainText>
            </Password>
          </LocalAccount>
        </LocalAccounts>"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage>
        <UILanguage>{language}</UILanguage>
      </SetupUILanguage>
      <InputLocale>{input_locale}</InputLocale>
      <SystemLocale>{language}</SystemLocale>
      <UserLocale>{language}</UserLocale>
      <UILanguage>{language}</UILanguage>
      <UILanguageFallback>{language}</UILanguageFallback>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <!-- System Reserved Partition -->
            <CreatePartition wcm:action="add">
              <Order>1</Order>
              <Type>Primary</Type>
              <Size>500</Size>
            </CreatePartition>
            <!-- Main OS Partition -->
            <CreatePartition wcm:action="add">
              <Order>2</Order>
              <Type>Primary</Type>
              <Extend>true</Extend>
            </CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add">
              <Order>1</Order>
              <PartitionID>1</PartitionID>
              <Format>NTFS</Format>
              <Label>System Reserved</Label>
              <Active>true</Active>
            </ModifyPartition>
            <ModifyPartition wcm:action="add">
              <Order>2</Order>
              <PartitionID>2</PartitionID>
              <Format>NTFS</Format>
              <Label>Windows</Label>
              <Letter>C</Letter>
            </ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <ProductKey>
          <Key>{DEFAULT_SETUP_KEY}</Key>
          <WillShowUI>OnError</WillShowUI>
        </ProductKey>
      </UserData>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add">
              <Key>/IMAGE/NAME</Key>
              <Value>{image_name}</Value>
            </MetaData>
          </InstallFrom>
          <InstallTo>
            <DiskID>0</DiskID>
            <PartitionID>2</PartitionID>
          </InstallTo>
          <WillShowUI>OnError</WillShowUI>
        </OSImage>
      </ImageInstall>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Bypass TPM</Description>
          <Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Description>Bypass SecureBoot</Description>
          <Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Description>Bypass RAM</Description>
          <Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>4</Order>
          <Description>Bypass CPU</Description>
          <Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>5</Order>
          <Description>Bypass Storage</Description>
          <Path>reg.exe add "HKLM\\SYSTEM\\Setup\\LabConfig" /v BypassStorageCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>{input_locale}</InputLocale>
      <SystemLocale>{language}</SystemLocale>
      <UserLocale>{language}</UserLocale>
      <UILanguage>{language}</UILanguage>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>{input_locale}</InputLocale>
      <SystemLocale>{language}</SystemLocale>
      <UserLocale>{language}</UserLocale>
      <UILanguage>{language}</UILanguage>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipUserOOBE>true</SkipUserOOBE>
        <SkipMachineOOBE>true</SkipMachineOOBE>
      </OOBE>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>{local_accounts_xml}
      </UserAccounts>
      <AutoLogon>
        <Enabled>true</Enabled>
        <LogonCount>5</LogonCount>
        <Username>{username}</Username>
        <Password>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </Password>
      </AutoLogon>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Run Guest Tools and QGA Setup Batch File</Description>
          <CommandLine>cmd.exe /c powershell -ExecutionPolicy Bypass -NoProfile -Command "Get-ChildItem -Path C:\\,D:\\,E:\\,F:\\,G:\\ -Filter setup.cmd -Recurse -ErrorAction SilentlyContinue | ForEach-Object {{ &amp; $_.FullName }}"</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Description>Reboot to finalize drivers and clear pending updates</Description>
          <CommandLine>cmd.exe /c shutdown /r /t 5 /f</CommandLine>
        </SynchronousCommand>
      </FirstLogonCommands>
      <TimeZone>{time_zone}</TimeZone>
    </component>
  </settings>
</unattend>"""


def get_qemu_ga_msi_path() -> Path | None:
    """Download and cache qemu-ga-x86_64.msi for QEMU Guest Agent installation."""
    from windows.iso import download_file, get_default_cache_dir

    cache_dir = get_default_cache_dir()
    msi_path = cache_dir / "qemu-ga-x86_64.msi"
    if msi_path.exists() and msi_path.stat().st_size > 0:
        return msi_path

    url = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-qemu-ga/qemu-ga-x86_64.msi"
    try:
        download_file(url, msi_path)
        if msi_path.exists() and msi_path.stat().st_size > 0:
            return msi_path
    except Exception as exc:
        print(f"[windows] Warning: Could not download qemu-ga-x86_64.msi: {exc}")
    return None


def get_virtio_guest_tools_path() -> Path | None:
    """Download and cache virtio-win-guest-tools.exe for VirtIO serial driver installation."""
    from windows.iso import download_file, get_default_cache_dir

    cache_dir = get_default_cache_dir()
    tools_path = cache_dir / "virtio-win-guest-tools.exe"
    if tools_path.exists() and tools_path.stat().st_size > 0:
        return tools_path

    url = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win-guest-tools.exe"
    try:
        download_file(url, tools_path)
        if tools_path.exists() and tools_path.stat().st_size > 0:
            return tools_path
    except Exception as exc:
        print(f"[windows] Warning: Could not download virtio-win-guest-tools.exe: {exc}")
    return None


def save_unattend_xml(
    target_dir: Path,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    computer_name: str = "WINDOWS-WINVM",
    language: str = "en-US",
    input_locale: str = "0409:00000409",
    time_zone: str = "UTC",
    image_name: str = "Windows 10 Pro",
) -> Path:
    """Save autounattend.xml into the specified target directory with casing variants and installer binaries."""
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / "autounattend.xml"
    content = generate_unattend_xml(
        username=username,
        password=password,
        computer_name=computer_name,
        language=language,
        input_locale=input_locale,
        time_zone=time_zone,
        image_name=image_name,
    )
    file_path.write_text(content, encoding="utf-8")

    # Save capitalization variants to guarantee match
    (target_dir / "AUTOUNATTEND.XML").write_text(content, encoding="utf-8")
    (target_dir / "Unattend.xml").write_text(content, encoding="utf-8")
    (target_dir / "unattend.xml").write_text(content, encoding="utf-8")
    (target_dir / "setup.cmd").write_text(SETUP_GUEST_CMD, encoding="utf-8")

    # Copy guest tools & QGA installer into target_dir
    msi_path = get_qemu_ga_msi_path()
    if msi_path and msi_path.exists():
        try:
            shutil.copyfile(msi_path, target_dir / "qemu-ga-x86_64.msi")
        except Exception:
            pass

    tools_path = get_virtio_guest_tools_path()
    if tools_path and tools_path.exists():
        try:
            shutil.copyfile(tools_path, target_dir / "virtio-win-guest-tools.exe")
        except Exception:
            pass

    return file_path


def create_unattend_iso(
    target_iso_path: Path,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    computer_name: str = "WINDOWS-WINVM",
    language: str = "en-US",
    input_locale: str = "0409:00000409",
    time_zone: str = "UTC",
    image_name: str = "Windows 10 Pro",
) -> Path:
    """Create a secondary ISO 9660 image containing autounattend.xml, qemu-ga installer, and virtio tools."""
    target_iso_path = Path(target_iso_path).resolve()
    target_iso_path.parent.mkdir(parents=True, exist_ok=True)

    xml_content = generate_unattend_xml(
        username=username,
        password=password,
        computer_name=computer_name,
        language=language,
        input_locale=input_locale,
        time_zone=time_zone,
        image_name=image_name,
    )
    xml_bytes = xml_content.encode("utf-8")

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3)

    iso.add_fp(
        io.BytesIO(xml_bytes),
        len(xml_bytes),
        iso_path="/AUTOUNAT.XML;1",
        joliet_path="/autounattend.xml",
    )

    cmd_bytes = SETUP_GUEST_CMD.encode("utf-8")
    iso.add_fp(
        io.BytesIO(cmd_bytes),
        len(cmd_bytes),
        iso_path="/SETUP.CMD;1",
        joliet_path="/setup.cmd",
    )

    msi_file = get_qemu_ga_msi_path()
    if msi_file and msi_file.exists():
        msi_bytes = msi_file.read_bytes()
        iso.add_fp(
            io.BytesIO(msi_bytes),
            len(msi_bytes),
            iso_path="/QEMU_GA.MSI;1",
            joliet_path="/qemu-ga-x86_64.msi",
        )

    tools_file = get_virtio_guest_tools_path()
    if tools_file and tools_file.exists():
        tools_bytes = tools_file.read_bytes()
        iso.add_fp(
            io.BytesIO(tools_bytes),
            len(tools_bytes),
            iso_path="/VIRT_TOOLS.EXE;1",
            joliet_path="/virtio-win-guest-tools.exe",
        )

    iso.write(str(target_iso_path))
    iso.close()
    return target_iso_path


# ---------------------------------------------------------------------------
# Windows XP provisioning: legacy winnt.sif answer file + FAT12 unattend floppy
# ---------------------------------------------------------------------------

# Retail product key published for the Windows XP Professional SP1 (x86,
# English, Retail) MSDN media linked from windows.iso. Override for XP
# SP2/SP3 media via the `product_key` argument.
DEFAULT_XP_PRODUCT_KEY = "X3WJB-3B2BH-3MPM6-8F6GR-X9HBJ"

XP_DEFAULT_COMPUTER_NAME = "WINXP-VM"

# Windows XP time zone registry indices (tzres family); anything unmapped
# falls back to GMT (index 110).
XP_TIME_ZONE_INDEX_MAP: dict[str, int] = {
    "utc": 110,
    "gmt": 110,
    "etc/utc": 110,
    "europe/london": 85,
    "gb": 85,
    "europe/jerusalem": 140,
    "asia/jerusalem": 140,
    "israel": 140,
    "america/new_york": 35,
    "eastern": 35,
    "america/los_angeles": 4,
    "pacific": 4,
}


def xp_time_zone_index(time_zone: str) -> int:
    """Map a python/locale style time zone name to a Windows XP time zone index (default GMT=110)."""
    return XP_TIME_ZONE_INDEX_MAP.get(str(time_zone).strip().lower(), 110)


def format_xp_product_id(product_key: str) -> str:
    """Normalize a 25-char XP product key into dash-separated XXXXX-XXXXX-... form used by ProductID."""
    raw = str(product_key).replace("-", "").replace(" ", "").upper()
    if len(raw) != 25 or not raw.isalnum():
        raise ValueError(
            f"Invalid XP product key: {product_key!r}. "
            "Expected 25 alphanumeric characters (e.g. XXXXX-XXXXX-XXXXX-XXXXX-XXXXX)."
        )
    return "-".join(raw[i : i + 5] for i in range(0, 25, 5))


def _sanitize_sif_value(value: str) -> str:
    """Strip characters that break winnt.sif parsing (quotes, newlines, semicolons)."""
    return str(value).replace('"', "").replace("\n", " ").replace(";", "").strip()


def render_winnt_sif(
    product_key: str = DEFAULT_XP_PRODUCT_KEY,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    computer_name: str = XP_DEFAULT_COMPUTER_NAME,
    full_name: str | None = None,
    org_name: str = "",
    time_zone: str = "UTC",
    workgroup: str = "WORKGROUP",
    target_path: str = r"\WINDOWS",
    skip_finalize: bool = True,
) -> str:
    """Generate the legacy winnt.sif unattended answer file for Windows XP (NT 5.1).

    The XP analog of generate_unattend_xml() for the pre-Vista setup engine.
    Attach the answer file inside a floppy image (create_xp_unattend_floppy) and
    boot the stock XP CD: setup polls A:\\WINNT.SIF and runs fully unattended.
    """
    full_name = full_name or username
    if full_name.strip().lower() in {"administrator", "guest"}:
        full_name = DEFAULT_XP_FULL_NAME
    return f"""[Data]
AutoPartition=1
MsDosInitiated="0"
UnattendedInstall="Yes"

[Unattended]
UnattendMode=FullUnattended
OemSkipEula=Yes
OemPreinstall=No
UnattendSwitch={"Yes" if skip_finalize else "No"}
Repartition=Yes
TargetPath={_sanitize_sif_value(target_path)}
WaitForReboot="No"

[GuiUnattended]
AdminPassword={_sanitize_sif_value(password)}
EncryptedAdminPassword="No"
OEMSkipRegional=1
OemSkipWelcome=1
TimeZone={xp_time_zone_index(time_zone)}

[UserData]
FullName="{_sanitize_sif_value(full_name)}"
OrgName="{_sanitize_sif_value(org_name)}"
ComputerName={_sanitize_sif_value(computer_name).replace(" ", "") or XP_DEFAULT_COMPUTER_NAME}
ProductID={format_xp_product_id(product_key)}

[Identification]
JoinWorkgroup={_sanitize_sif_value(workgroup).upper() or "WORKGROUP"}

[Networking]
InstallDefaultComponents=Yes
"""


def _set_fat12_entry(fat: bytearray, cluster: int, value: int) -> None:
    """Write a 12-bit cluster entry into a raw FAT12 table (little-endian nibble packing)."""
    offset = (3 * cluster) // 2
    if cluster % 2 == 0:
        fat[offset] = value & 0xFF
        fat[offset + 1] = (fat[offset + 1] & 0xF0) | ((value >> 8) & 0x0F)
    else:
        fat[offset] = (fat[offset] & 0xF0) | ((value & 0x0F) << 4)
        fat[offset + 1] = (value >> 4) & 0xFF


def _fat12_floppy_image(files: dict[str, bytes]) -> bytes:
    """Build a raw 1.44MB FAT12 floppy image containing the given 8.3 named files.

    Pure-Python MS-DOS floppy image builder (1,474,560 bytes, 512B sectors,
    80 tracks x 2 heads x 18 sectors/track, 2 FATs of 9 sectors, 224 root
    entries). File keys must be plain DOS 8.3 basenames (e.g. 'WINNT.SIF').
    """
    SECTOR = 512
    TOTAL_SECTORS = 2880
    SPC = 1
    RESERVED = 1
    N_FATS = 2
    FAT_SECTORS = 9
    ROOT_ENTRIES = 224

    fat_offset = RESERVED * SECTOR
    root_offset = fat_offset + N_FATS * FAT_SECTORS * SECTOR
    root_size = ROOT_ENTRIES * 32
    data_offset = root_offset + root_size
    last_usable_cluster = 1 + (TOTAL_SECTORS - (data_offset // SECTOR)) // SPC  # cluster 2848

    img = bytearray(TOTAL_SECTORS * SECTOR)

    # BIOS Parameter Block (classic 1.44MB floppy geometry)
    bpb = bytearray()
    bpb += b"\xeb\x3c\x90"  # jmp short + nop
    bpb += b"MSDOS5.0"  # OEM name
    bpb += SECTOR.to_bytes(2, "little")  # bytes per sector
    bpb += SPC.to_bytes(1, "little")  # sectors per cluster
    bpb += RESERVED.to_bytes(2, "little")
    bpb += N_FATS.to_bytes(1, "little")
    bpb += ROOT_ENTRIES.to_bytes(2, "little")
    bpb += TOTAL_SECTORS.to_bytes(2, "little")
    bpb += b"\xf0"  # media descriptor (removable)
    bpb += FAT_SECTORS.to_bytes(2, "little")
    bpb += (18).to_bytes(2, "little")  # sectors per track
    bpb += (2).to_bytes(2, "little")  # heads
    bpb += (0).to_bytes(4, "little")  # hidden sectors
    bpb += (0).to_bytes(4, "little")  # total sectors (32-bit; zero for 16-bit layout)
    bpb += (0x00).to_bytes(1, "little")  # drive number
    bpb += (0).to_bytes(1, "little")  # reserved
    bpb += (0x29).to_bytes(1, "little")  # extended boot signature
    bpb += (0x00A1B2C3).to_bytes(4, "little")  # volume serial
    bpb += b"NO NAME    "  # volume label
    bpb += b"FAT12   "  # fs type
    boot_sector = bpb + (b"\x00" * (SECTOR - len(bpb) - 2)) + b"\x55\xaa"
    img[0:SECTOR] = boot_sector

    fat = bytearray(FAT_SECTORS * SECTOR)
    fat[0] = 0xF0  # media descriptor mirrored in FAT[0]
    fat[1] = 0xFF
    fat[2] = 0xFF

    dir_entries = bytearray(ROOT_ENTRIES * 32)
    data = bytearray(TOTAL_SECTORS * SECTOR - data_offset)
    next_free_cluster = 2

    for idx, (name, content) in enumerate(files.items()):
        stem, ext = ([*name.split(".", 1), ""])[:2] if "." in name else (name, "")
        stem = (stem or "_").upper()[:8].ljust(8)
        ext = ext.upper()[:3].ljust(3)
        n_clusters = max(1, -(-len(content) // (SPC * SECTOR)))
        first_cluster = next_free_cluster
        if first_cluster + n_clusters - 1 > last_usable_cluster:
            raise ValueError(f"Floppy image overflow for file {name!r}")
        start = (first_cluster - 2) * SPC * SECTOR
        data[start : start + len(content)] = content
        for i in range(n_clusters):
            cluster = first_cluster + i
            _set_fat12_entry(fat, cluster, 0xFFF if i == n_clusters - 1 else cluster + 1)
        next_free_cluster += n_clusters

        entry = bytearray(32)
        entry[0:8] = stem.encode("ascii")
        entry[8:11] = ext.encode("ascii")
        entry[11] = 0x20  # archive attribute
        entry[26:28] = first_cluster.to_bytes(2, "little")
        entry[28:32] = len(content).to_bytes(4, "little")
        dir_entries[idx * 32 : idx * 32 + 32] = entry

    img[fat_offset : fat_offset + FAT_SECTORS * SECTOR] = fat
    img[fat_offset + FAT_SECTORS * SECTOR : fat_offset + 2 * FAT_SECTORS * SECTOR] = fat
    img[root_offset : root_offset + root_size] = dir_entries
    img[data_offset:] = data
    return bytes(img)


def create_xp_unattend_floppy(
    output_path: str | Path = "unattend_floppy.img",
    product_key: str = DEFAULT_XP_PRODUCT_KEY,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    computer_name: str = XP_DEFAULT_COMPUTER_NAME,
    full_name: str | None = None,
    time_zone: str = "UTC",
    workgroup: str = "WORKGROUP",
) -> Path:
    """Create a 1.44MB FAT12 floppy disk image containing the XP winnt.sif answer file.

    Booting the stock XP CD with this floppy attached (`-fda`) makes both the
    text-mode and GUI phases of XP setup run fully unattended (no interaction).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = render_winnt_sif(
        product_key=product_key,
        username=username,
        password=password,
        computer_name=computer_name,
        full_name=full_name,
        time_zone=time_zone,
        workgroup=workgroup,
    ).replace("\n", "\r\n")
    image = _fat12_floppy_image({"WINNT.SIF": content.encode("ascii", errors="replace")})
    output_path.write_bytes(image)
    return output_path
