"""Unit tests for Windows XP legacy unattended provisioning helpers.

Covers the legacy winnt.sif answer-file renderer and the pure-Python FAT12
floppy image builder used to feed WINNT.SIF to the stock XP CD setup.
"""

import struct

import pytest

from windows.iso import VERSION_ALIAS_MAP, WINDOWS_VERSION_URLS, WindowsVersion, _normalized_version_member
from windows.unattend import (
    DEFAULT_XP_FULL_NAME,
    DEFAULT_XP_PRODUCT_KEY,
    XP_DEFAULT_COMPUTER_NAME,
    _fat12_floppy_image,
    create_xp_unattend_floppy,
    format_xp_product_id,
    render_winnt_sif,
    xp_time_zone_index,
)

# ---------------------------------------------------------------------------
# winnt.sif renderer
# ---------------------------------------------------------------------------


def test_render_winnt_sif_defaults():
    sif = render_winnt_sif()
    assert "[Data]" in sif
    assert "AutoPartition=1" in sif
    assert 'UnattendedInstall="Yes"' in sif
    assert "UnattendMode=FullUnattended" in sif
    assert "OemSkipEula=Yes" in sif
    assert "UnattendSwitch=Yes" in sif
    assert "Repartition=Yes" in sif
    assert "TargetPath=\\WINDOWS" in sif
    assert "OemSkipWelcome=1" in sif
    assert f"ProductID={format_xp_product_id(DEFAULT_XP_PRODUCT_KEY)}" in sif
    assert f"ComputerName={XP_DEFAULT_COMPUTER_NAME}" in sif
    assert "JoinWorkgroup=WORKGROUP" in sif
    assert "InstallDefaultComponents=Yes" in sif


def test_render_winnt_sif_rejects_reserved_full_names():
    # XP GUI setup errors out on FullName="Administrator" / "Guest"
    for reserved in ("Administrator", "administrator", "Guest"):
        sif = render_winnt_sif(full_name=reserved)
        assert f'FullName="{reserved}"' not in sif
        assert f'FullName="{DEFAULT_XP_FULL_NAME}"' in sif

    # Same protection applies when the name comes from username
    sif = render_winnt_sif(username="Administrator", full_name=None)
    assert f'FullName="{DEFAULT_XP_FULL_NAME}"' in sif


def test_render_winnt_sif_valid_full_name_allowed():
    sif = render_winnt_sif(full_name="Gilad")
    assert 'FullName="Gilad"' in sif
    sif = render_winnt_sif(username="Administrator", full_name="Gilad Beeri")
    assert 'FullName="Gilad Beeri"' in sif


def test_render_winnt_sif_custom_fields():
    sif = render_winnt_sif(
        product_key="ABCDE-FGHIJ-KLMNO-PQRST-VWXYZ",
        password="S3cret!",
        computer_name="XPTESTBOX",
        time_zone="Europe/Jerusalem",
        workgroup="lab",
        target_path="\\WXP",
        skip_finalize=False,
    )
    assert "ProductID=ABCDE-FGHIJ-KLMNO-PQRST-VWXYZ" in sif
    assert "AdminPassword=S3cret!" in sif
    assert "ComputerName=XPTESTBOX" in sif
    assert f"TimeZone={xp_time_zone_index('europe/jerusalem')}" in sif
    assert "JoinWorkgroup=LAB" in sif
    assert "TargetPath=\\WXP" in sif
    assert "UnattendSwitch=No" in sif


def test_render_winnt_sif_sanitizes_quotes_in_values():
    sif = render_winnt_sif(full_name='Evil"Name')
    assert 'FullName="EvilName"' in sif


def test_format_xp_product_id():
    assert format_xp_product_id("X3WJB-3B2BH-3MPM6-8F6GR-X9HBJ") == "X3WJB-3B2BH-3MPM6-8F6GR-X9HBJ"
    assert format_xp_product_id("x3wjb3b2bh3mpm68f6grx9hbj") == "X3WJB-3B2BH-3MPM6-8F6GR-X9HBJ"
    with pytest.raises(ValueError):
        format_xp_product_id("SHORT-KEY")
    with pytest.raises(ValueError):
        format_xp_product_id("!@#%!@#%!@#%!@#%!@#$")


def test_xp_time_zone_index():
    assert xp_time_zone_index("UTC") == 110
    assert xp_time_zone_index("GMT") == 110
    assert xp_time_zone_index("Europe/Jerusalem") == 140
    assert xp_time_zone_index("America/New_York") == 35
    assert xp_time_zone_index("Mars/Olympus_Mons") == 110  # unknown falls back to GMT


# ---------------------------------------------------------------------------
# FAT12 floppy image builder
# ---------------------------------------------------------------------------


SECTOR = 512
FAT_OFFSET = SECTOR
FAT_SECTORS = 9
ROOT_OFFSET = FAT_OFFSET + 2 * FAT_SECTORS * SECTOR
DATA_OFFSET = ROOT_OFFSET + 224 * 32


def _dir_entries_for(img: bytes, name: str) -> int:
    """Return the starting cluster stored in the first directory entry matching name."""
    stem, ext = name.split(".")
    needle = stem.upper().ljust(8).encode() + ext.upper().ljust(3).encode()
    for offset in range(ROOT_OFFSET, ROOT_OFFSET + 224 * 32, 32):
        if img[offset : offset + 11] == needle:
            return struct.unpack_from("<H", img, offset + 26)[0]
    raise AssertionError(f"directory entry for {name} not found")


def test_fat12_floppy_size_and_boot_sector(tmp_path):
    out = create_xp_unattend_floppy(tmp_path / "f.img")
    img = out.read_bytes()
    assert len(img) == 1_474_560  # exact 1.44MB DD floppy size
    assert img[SECTOR - 2 : SECTOR] == b"\x55\xaa"  # boot signature
    bpb = img[11:25]
    assert struct.unpack_from("<H", bpb, 0)[0] == 512  # bytes/sector
    assert bpb[2] == 1  # sectors/cluster
    assert struct.unpack_from("<H", bpb, 3)[0] == 1  # reserved sectors
    assert bpb[5] == 2  # number of FATs
    assert struct.unpack_from("<H", bpb, 6)[0] == 224  # root entries
    assert struct.unpack_from("<H", bpb, 8)[0] == 2880  # total sectors
    assert struct.unpack_from("<H", bpb, 11)[0] == FAT_SECTORS  # sectors/FAT
    assert img[54 : 54 + 8] == b"FAT12   "


def test_fat12_floppy_contains_winnt_sif_content(tmp_path):
    sif = render_winnt_sif(computer_name="XPTESTBOX").replace("\n", "\r\n")
    img = create_xp_unattend_floppy(tmp_path / "f.img", computer_name="XPTESTBOX").read_bytes()
    cluster = _dir_entries_for(img, "WINNT.SIF")
    assert cluster >= 2

    # Follow the FAT12 chain to reconstruct the file content
    size = struct.unpack_from("<I", img, ROOT_OFFSET + 28)[0]
    fat = img[FAT_OFFSET : FAT_OFFSET + FAT_SECTORS * SECTOR]

    def fat_entry(idx: int) -> int:
        offset = (3 * idx) // 2
        if idx % 2 == 0:
            return (fat[offset] | (fat[offset + 1] << 8)) & 0xFFF
        return ((fat[offset] >> 4) | (fat[offset + 1] << 4)) & 0xFFF

    data = bytearray()
    idx = cluster
    while idx < 0xFF8:
        start = DATA_OFFSET + (idx - 2) * SECTOR
        data += img[start : start + SECTOR]
        idx = fat_entry(idx)
        assert idx != 0, "FAT chain broken"
    assert bytes(data[:size]) == sif.encode()


def test_fat12_floppy_multiple_files_chained():
    files = {"WINNT.SIF": b"a" * 3_500, "README.TXT": b"hello"}  # sif spans 7 clusters
    img = bytes(_fat12_floppy_image(files))
    start_a = _dir_entries_for(img, "WINNT.SIF")
    start_b = _dir_entries_for(img, "README.TXT")
    # Files must not share clusters: the readme lands after the 7-cluster sif file
    assert start_b == start_a + 7
    data_b = img[DATA_OFFSET + (start_b - 2) * SECTOR :][: len(files["README.TXT"])]
    assert data_b == files["README.TXT"]


def test_fat12_floppy_overflow_rejected():
    with pytest.raises(ValueError, match="overflow"):
        _fat12_floppy_image({"WINNT.SIF": b"x" * 2_000_000})


def test_create_xp_unattend_floppy_writes_file(tmp_path):
    out_tmp = tmp_path / "floppy" / "unattend.img"
    path = create_xp_unattend_floppy(out_tmp, computer_name="XPTESTBOX", full_name="Gilad")
    assert path == out_tmp
    raw = out_tmp.read_bytes()
    assert len(raw) == 1_474_560
    img = bytes(raw)
    cluster = _dir_entries_for(img, "WINNT.SIF")
    size = struct.unpack_from("<I", img, ROOT_OFFSET + 28)[0]
    content = img[DATA_OFFSET + (cluster - 2) * SECTOR :][:size]
    assert b"ComputerName=XPTESTBOX" in content
    assert b'FullName="Gilad"' in content


# ---------------------------------------------------------------------------
# ISO version routing helpers
# ---------------------------------------------------------------------------


def test_there_are_isos_for_xp_versions():
    for version in ("XP_SP1", "XP_SP1_X64", "XP_SP2", "XP_SP3"):
        member = WindowsVersion[version]
        assert WINDOWS_VERSION_URLS[member]
        assert all(url.endswith(".iso") for url in WINDOWS_VERSION_URLS[member])


def test_is_xp_matches():
    assert WindowsVersion.is_xp(WindowsVersion.XP_SP1)
    assert WindowsVersion.is_xp(WindowsVersion.XP_SP3)
    assert WindowsVersion.is_xp("winxp")
    assert WindowsVersion.is_xp("xp3")
    assert not WindowsVersion.is_xp(WindowsVersion.WIN10_22H2)
    assert not WindowsVersion.is_xp("win11_24h2")
    assert not WindowsVersion.is_xp(None)
    assert not WindowsVersion.is_xp("definitely-not-a-version")


def test_alias_map_has_xp_sp1():
    assert VERSION_ALIAS_MAP["xp"] == WindowsVersion.XP_SP1
    assert VERSION_ALIAS_MAP["xp_sp1"] == WindowsVersion.XP_SP1
    assert _normalized_version_member("winxp_sp1") == WindowsVersion.XP_SP1
    with pytest.raises(ValueError):
        _normalized_version_member("not-a-version")


def test_windows_by_alias_sp3():
    assert VERSION_ALIAS_MAP["winxp_sp3"] == WindowsVersion.XP_SP3
