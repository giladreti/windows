"""Unattended XML (autounattend.xml) generator and ISO builder for automated Windows installation."""

import io
import shutil
from pathlib import Path

import pycdlib

DEFAULT_USERNAME = "Administrator"
DEFAULT_PASSWORD = "Password123!"
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
