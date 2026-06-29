"""Suporte ao mhvtl (Virtual Tape Library).

Documentação de referência:
  https://github.com/markh794/mhvtl
  man 5 device.conf  /  man 5 mhvtl.conf  /  man 5 library_contents

Funcionalidades:
  - detectar se o mhvtl está ativo (módulo kernel + /etc/mhvtl)
  - parser de mhvtl.conf -> path do diretório de configuração
  - parser de device.conf  -> Libraries e Drives com Vendor/Model/Serial/NAA/C:T:L
  - parser de library_contents.<N> -> slots/drives/MAPs/pickers
  - conversão NAA -> WWN -> ID_SERIAL (compatível com udev ID_SERIAL_SHORT)
  - correlacionar Drives do device.conf com /dev/sg*/dev/nst* via:
      * NAA/ID_SERIAL (mais robusto - sobrevive a reboots)
      * C:T:L        (fallback se NAA não bater)
      * Unit serial number (fallback)
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .logger import get_logger


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class MhvtlLibrary:
    """Entrada `Library: N` em /etc/mhvtl/device.conf."""
    index: int
    channel: int
    target: int
    lun: int
    vendor: str = ""
    product: str = ""
    revision: str = ""
    serial: str = ""
    naa: str = ""
    home_dir: str = ""
    persist: bool = False
    backoff: int = 0
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def wwn(self) -> str:
        return naa_to_wwn(self.naa) if self.naa else ""

    @property
    def id_serial(self) -> str:
        wwn = self.wwn
        return ("3" + wwn) if wwn else ""


@dataclass
class MhvtlDrive:
    """Entrada `Drive: N` em /etc/mhvtl/device.conf."""
    index: int
    channel: int
    target: int
    lun: int
    library_id: int | None = None
    slot: int | None = None
    vendor: str = ""
    product: str = ""
    revision: str = ""
    serial: str = ""
    naa: str = ""
    compression_factor: int = 0
    compression_enabled: bool = False
    compression_type: str = ""
    backoff: int = 0
    raw: dict[str, str] = field(default_factory=dict)
    # Preenchido em runtime ao correlacionar com /dev/*
    nst_device: str | None = None
    st_device: str | None = None
    sg_device: str | None = None
    drive_number: int | None = None

    @property
    def ctl(self) -> str:
        return f"{self.channel}:{self.target}:{self.lun}"

    @property
    def wwn(self) -> str:
        return naa_to_wwn(self.naa) if self.naa else ""

    @property
    def id_serial(self) -> str:
        wwn = self.wwn
        return ("3" + wwn) if wwn else ""


@dataclass
class MhvtlLibraryContents:
    """Conteúdo de library_contents.<N>."""
    library_index: int
    drives: list[tuple[int, str]] = field(default_factory=list)
    pickers: list[tuple[int, str]] = field(default_factory=list)
    maps: list[tuple[int, str]] = field(default_factory=list)
    slots: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class MhvtlConfig:
    """Resultado do parser de /etc/mhvtl/."""
    config_path: str = "/etc/mhvtl"
    home_path: str = "/opt/vtl"
    capacity_mb: int = 500
    verbose: int = 1
    vtl_debug: int = 0
    libraries: list[MhvtlLibrary] = field(default_factory=list)
    drives: list[MhvtlDrive] = field(default_factory=list)
    contents: dict[int, MhvtlLibraryContents] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Conversão NAA -> WWN -> ID_SERIAL (igual ao mhvtl kernel)
# ---------------------------------------------------------------------------
def naa_to_wwn(naa: str) -> str:
    """Converte NAA do device.conf para WWN conforme o mhvtl faz em
    update_vpd_83(): força o high nibble do primeiro byte para 5.
    """
    if not naa:
        return ""
    try:
        raw = bytes.fromhex(naa.replace(":", "").strip())
    except ValueError:
        return ""
    if not raw:
        return ""
    raw = bytes([(raw[0] & 0x0F) | 0x50]) + raw[1:]
    return raw.hex()


def naa_to_id_serial(naa: str) -> str:
    """ID_SERIAL como o udev scsi_id gera (prefixo '3' = NAA designator)."""
    wwn = naa_to_wwn(naa)
    return ("3" + wwn) if wwn else ""


# ---------------------------------------------------------------------------
# Detecção de mhvtl ativo
# ---------------------------------------------------------------------------
def is_mhvtl_loaded() -> bool:
    """Verifica se o módulo kernel mhvtl está carregado."""
    if Path("/sys/module/mhvtl").is_dir():
        return True
    if Path("/sys/bus/pseudo/drivers/mhvtl").is_dir():
        return True
    try:
        proc = subprocess.run(
            ["lsmod"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=5, check=False,
        )
        if proc.returncode == 0 and re.search(r"^mhvtl\s", proc.stdout, re.MULTILINE):
            return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return False


def find_mhvtl_config_dir(override: str | None = None) -> str | None:
    """Retorna o diretório de configuração do mhvtl."""
    if override:
        p = Path(override)
        if (p / "device.conf").is_file():
            return str(p)
        return None

    mhvtl_conf = Path("/etc/mhvtl/mhvtl.conf")
    if mhvtl_conf.is_file():
        try:
            for line in mhvtl_conf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "MHVTL_CONFIG_PATH":
                    cand = v.strip().strip('"').strip("'")
                    if Path(cand, "device.conf").is_file():
                        return cand
        except OSError:
            pass

    if Path("/etc/mhvtl/device.conf").is_file():
        return "/etc/mhvtl"
    return None


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
_HEADER_LIB_RE = re.compile(
    r"^Library:\s+(\d+)\s+CHANNEL:\s+(\d+)\s+TARGET:\s+(\d+)\s+LUN:\s+(\d+)"
)
_HEADER_DRV_RE = re.compile(
    r"^Drive:\s+(\d+)\s+CHANNEL:\s+(\d+)\s+TARGET:\s+(\d+)\s+LUN:\s+(\d+)"
)


def parse_mhvtl_conf(path: str = "/etc/mhvtl/mhvtl.conf") -> dict[str, str]:
    """Parser simples de mhvtl.conf (key=value)."""
    result: dict[str, str] = {
        "MHVTL_CONFIG_PATH": "/etc/mhvtl",
        "MHVTL_HOME_PATH": "/opt/vtl",
        "CAPACITY": "500",
        "VERBOSE": "1",
        "VTL_DEBUG": "0",
        "DAEMON_DEBUG": "",
    }
    p = Path(path)
    if not p.is_file():
        return result
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return result


def parse_device_conf(config_path: str) -> tuple[list[MhvtlLibrary], list[MhvtlDrive]]:
    """Parser de /etc/mhvtl/device.conf. Retorna (libraries, drives)."""
    p = Path(config_path) / "device.conf"
    if not p.is_file():
        return [], []
    text = p.read_text(encoding="utf-8", errors="replace")

    libraries: list[MhvtlLibrary] = []
    drives: list[MhvtlDrive] = []

    sections = re.split(r"\n[ \t]*\n", text)
    for sec in sections:
        sec = sec.strip()
        if not sec or sec.startswith("#") or sec.startswith("VERSION:"):
            continue

        m_lib = _HEADER_LIB_RE.match(sec)
        m_drv = _HEADER_DRV_RE.match(sec)

        if m_lib:
            lib = _build_library(sec, m_lib)
            libraries.append(lib)
        elif m_drv:
            drv = _build_drive(sec, m_drv)
            drives.append(drv)

    return libraries, drives


def _build_library(sec: str, m: re.Match) -> MhvtlLibrary:
    lib = MhvtlLibrary(
        index=int(m.group(1)),
        channel=int(m.group(2)),
        target=int(m.group(3)),
        lun=int(m.group(4)),
    )
    for line in sec.splitlines()[1:]:
        line = line.lstrip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        lib.raw[key] = val
        if key == "Vendor identification":
            lib.vendor = val
        elif key == "Product identification":
            lib.product = val
        elif key == "Product revision level":
            lib.revision = val
        elif key == "Unit serial number":
            lib.serial = val
        elif key == "NAA":
            lib.naa = val
        elif key == "Home directory":
            lib.home_dir = val
        elif key == "PERSIST":
            lib.persist = val.lower() in ("true", "yes", "1")
        elif key == "Backoff":
            try:
                lib.backoff = int(val)
            except ValueError:
                pass
    return lib


def _build_drive(sec: str, m: re.Match) -> MhvtlDrive:
    drv = MhvtlDrive(
        index=int(m.group(1)),
        channel=int(m.group(2)),
        target=int(m.group(3)),
        lun=int(m.group(4)),
    )
    for line in sec.splitlines()[1:]:
        line = line.lstrip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        drv.raw[key] = val
        if key == "Library ID":
            m_lib = re.search(r"(\d+)", val)
            if m_lib:
                drv.library_id = int(m_lib.group(1))
            m_slot = re.search(r"Slot\s*:?\s*(\d+)", val, re.IGNORECASE)
            if m_slot:
                drv.slot = int(m_slot.group(1))
        elif key == "Slot":
            if val.isdigit():
                drv.slot = int(val)
        elif key == "Vendor identification":
            drv.vendor = val
        elif key == "Product identification":
            drv.product = val
        elif key == "Product revision level":
            drv.revision = val
        elif key == "Unit serial number":
            drv.serial = val
        elif key == "NAA":
            drv.naa = val
        elif key == "Compression":
            mf = re.search(r"factor\s+(\d+)", val)
            me = re.search(r"enabled\s+(\d+)", val)
            if mf:
                drv.compression_factor = int(mf.group(1))
            if me:
                drv.compression_enabled = (me.group(1) == "1")
        elif key == "Compression type":
            drv.compression_type = val
        elif key == "Backoff":
            try:
                drv.backoff = int(val)
            except ValueError:
                pass
    return drv


def parse_library_contents(config_path: str, library_index: int) -> MhvtlLibraryContents:
    """Parser de library_contents.<N>."""
    contents = MhvtlLibraryContents(library_index=library_index)
    p = Path(config_path) / f"library_contents.{library_index}"
    if not p.is_file():
        return contents

    pattern = re.compile(r"^(Drive|Picker|MAP|Slot)\s+(\d+):\s*(.*)$")
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("VERSION:"):
                continue
            m = pattern.match(line)
            if not m:
                continue
            kind, num, val = m.group(1), int(m.group(2)), m.group(3).strip()
            if kind == "Drive":
                contents.drives.append((num, val))
            elif kind == "Picker":
                contents.pickers.append((num, val))
            elif kind == "MAP":
                contents.maps.append((num, val))
            elif kind == "Slot":
                contents.slots.append((num, val))
    except OSError:
        pass
    return contents


# ---------------------------------------------------------------------------
# API principal: carrega tudo
# ---------------------------------------------------------------------------
def load_mhvtl(config_dir_override: str | None = None) -> MhvtlConfig | None:
    """Carrega todas as configurações mhvtl disponíveis.

    Retorna None se mhvtl não estiver instalado/detectado.
    """
    log = get_logger()

    if not is_mhvtl_loaded():
        log.debug("mhvtl kernel module não detectado.")

    config_dir = find_mhvtl_config_dir(config_dir_override)
    if not config_dir:
        return None

    log.info("mhvtl detectado: config_dir=%s", config_dir)

    mhvtl_conf = parse_mhvtl_conf(Path(config_dir) / "mhvtl.conf")
    libraries, drives = parse_device_conf(config_dir)

    contents: dict[int, MhvtlLibraryContents] = {}
    for lib in libraries:
        c = parse_library_contents(config_dir, lib.index)
        contents[lib.index] = c

    cfg = MhvtlConfig(
        config_path=config_dir,
        home_path=mhvtl_conf.get("MHVTL_HOME_PATH", "/opt/vtl"),
        capacity_mb=int(mhvtl_conf.get("CAPACITY", "500") or "500"),
        verbose=int(mhvtl_conf.get("VERBOSE", "1") or "1"),
        vtl_debug=int(mhvtl_conf.get("VTL_DEBUG", "0") or "0"),
        libraries=libraries,
        drives=drives,
        contents=contents,
    )
    log.info("mhvtl: %d libraries, %d drives", len(libraries), len(drives))
    return cfg


# ---------------------------------------------------------------------------
# Correlação com dispositivos /dev/*
# ---------------------------------------------------------------------------
def _udev_id_serial(dev_path: str) -> str:
    """Lê ID_SERIAL (ou ID_SERIAL_SHORT) via udevadm info."""
    try:
        proc = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", dev_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=5, check=False,
        )
        if proc.returncode != 0:
            return ""
        for line in proc.stdout.splitlines():
            if line.startswith("ID_SERIAL="):
                return line.split("=", 1)[1].strip()
            if line.startswith("ID_SERIAL_SHORT="):
                v = line.split("=", 1)[1].strip()
                return v
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return ""


def _sysfs_wwid(dev_path: str) -> str:
    """Lê /sys/class/scsi_generic/sgN/device/wwid (formato: naa.XXXX...)."""
    try:
        name = Path(dev_path).name
        wwid_path = Path("/sys/class/scsi_generic") / name / "device" / "wwid"
        if wwid_path.is_file():
            content = wwid_path.read_text().strip()
            if content.startswith("naa."):
                return content[4:]
            return content
        if name.startswith("nst"):
            wwid_path = Path("/sys/class/scsi_tape") / name / "device" / "wwid"
            if wwid_path.is_file():
                content = wwid_path.read_text().strip()
                if content.startswith("naa."):
                    return content[4:]
                return content
    except OSError:
        pass
    return ""


def _sysfs_ctl(dev_path: str) -> str | None:
    """Retorna C:T:L de um dispositivo via /sys/.../device/{channel,id,lun}."""
    try:
        name = Path(dev_path).name
        if name.startswith("sg"):
            base = Path("/sys/class/scsi_generic") / name / "device"
        elif name.startswith("nst"):
            base = Path("/sys/class/scsi_tape") / name / "device"
        elif name.startswith("st"):
            num = name.removeprefix("st")
            base = Path("/sys/class/scsi_tape") / f"nst{num}" / "device"
        else:
            return None
        if not base.is_dir():
            return None
        ch = (base / "channel").read_text().strip()
        tgt = (base / "id").read_text().strip()
        lun = (base / "lun").read_text().strip()
        return f"{ch}:{tgt}:{lun}"
    except OSError:
        return None


def _sysfs_sg_for_nst(nst_path: str) -> str | None:
    """Retorna /dev/sgN correspondente a /dev/nstN."""
    try:
        name = Path(nst_path).name
        gen = Path("/sys/class/scsi_tape") / name / "device" / "generic"
        if gen.exists():
            target = gen.resolve()
            sg_name = target.name
            if sg_name.startswith("sg"):
                return f"/dev/{sg_name}"
    except OSError:
        pass
    return None


def correlate_drives(cfg: MhvtlConfig) -> MhvtlConfig:
    """Preenche nst_device/st_device/sg_device/drive_number em cada MhvtlDrive."""
    log = get_logger()

    nst_info: dict[str, dict[str, str]] = {}
    for nst in sorted(Path("/dev").glob("nst*"), key=_natural_key):
        if not nst.is_char_device():
            continue
        nst_str = str(nst)
        nst_info[nst_str] = {
            "id_serial": _udev_id_serial(nst_str),
            "wwid": _sysfs_wwid(nst_str),
            "ctl": _sysfs_ctl(nst_str) or "",
            "sg": _sysfs_sg_for_nst(nst_str) or "",
        }

    sg_info: dict[str, dict[str, str]] = {}
    for sg in sorted(Path("/dev").glob("sg*"), key=_natural_key):
        if not sg.is_char_device():
            continue
        sg_str = str(sg)
        sg_info[sg_str] = {
            "id_serial": _udev_id_serial(sg_str),
            "wwid": _sysfs_wwid(sg_str),
            "ctl": _sysfs_ctl(sg_str) or "",
        }

    for drv in cfg.drives:
        target_wwn = drv.wwn
        target_id_serial = drv.id_serial
        matched_nst: str | None = None
        matched_sg: str | None = None

        if target_wwn or target_id_serial:
            for nst_str, info in nst_info.items():
                if (target_wwn and info["wwid"] == target_wwn) or \
                   (target_id_serial and
                    (info["id_serial"] == target_id_serial or
                     info["id_serial"].endswith(target_wwn))):
                    matched_nst = nst_str
                    matched_sg = info["sg"] or None
                    break

        if not matched_nst and drv.ctl:
            for nst_str, info in nst_info.items():
                if info["ctl"] == drv.ctl:
                    matched_nst = nst_str
                    matched_sg = info["sg"] or None
                    break

        if not matched_nst and drv.serial:
            for nst_str, info in nst_info.items():
                if drv.serial in info["id_serial"]:
                    matched_nst = nst_str
                    matched_sg = info["sg"] or None
                    break

        if matched_nst:
            drv.nst_device = matched_nst
            num = Path(matched_nst).name.removeprefix("nst")
            drv.st_device = f"/dev/st{num}"
            drv.drive_number = int(num) if num.isdigit() else None
            if not matched_sg:
                for sg_str, info in sg_info.items():
                    if (target_wwn and info["wwid"] == target_wwn) or \
                       (drv.ctl and info["ctl"] == drv.ctl):
                        matched_sg = sg_str
                        break
            drv.sg_device = matched_sg
            log.info(
                "mhvtl Drive %d (serial=%s) -> %s (sg=%s, drive#=%s)",
                drv.index, drv.serial, matched_nst, matched_sg, drv.drive_number,
            )
        else:
            log.warning(
                "mhvtl Drive %d (serial=%s, naa=%s, ctl=%s) NÃO correlacionado com /dev/*",
                drv.index, drv.serial, drv.naa, drv.ctl,
            )
    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _natural_key(s) -> list:
    name = str(s)
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]
