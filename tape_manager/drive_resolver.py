"""Resolvedor de drive por SERIAL NUMBER.

Garante identificação consistente do drive dedicado mesmo após reboot do
Linux (que pode renumerar /dev/nst0, /dev/nst1, ...).

Estratégia (em ordem):
  1. /dev/tape/by-id/  -> symlinks wwn-* / scsi-* contêm o serial SCSI
  2. lsscsi -t         -> correlaciona HCTL com /dev/st* e /dev/sg*
  3. sg_inq -s <dev>   -> retorna "Unit serial number:" para cada /dev/sg*
  4. /etc/mhvtl/device.conf (fallback mhvtl)

Saídas:
  - Retorna um DriveDevice com caminhos st (rewind) e nst (non-rewind)
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .exceptions import DriveNotFoundError
from .logger import get_logger


@dataclass
class DriveDevice:
    serial: str
    nst_device: str            # /dev/nstN (non-rewind) - preferencial
    st_device: str             # /dev/stN (rewind)
    sg_device: str | None = None
    drive_number: int = 0      # número lógico usado pelo mtx (0-based)

    @property
    def preferred(self) -> str:
        """Devolve o device preferido para gravação (non-rewind)."""
        return self.nst_device


class DriveResolver:
    _SERIAL_LEN = 8

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()
        self._cache: dict[str, DriveDevice] = {}

    # -----------------------------------------------------------------
    # API pública
    # -----------------------------------------------------------------
    def resolve(self, serial: str | None = None) -> DriveDevice:
        """Localiza o drive pelo serial number.

        Se `serial` for None, usa `cfg.drives.dedicated_serial`.
        Lança DriveNotFoundError se não encontrar.
        """
        if not serial:
            serial = self.cfg.drives.dedicated_serial
        if not serial:
            raise DriveNotFoundError(
                "Nenhum serial number configurado em drives.dedicated_serial."
            )
        serial_norm = serial.strip().upper()
        if serial_norm in self._cache:
            self.log.debug("Drive resolto via cache: %s -> %s",
                           serial_norm, self._cache[serial_norm])
            return self._cache[serial_norm]

        self.log.info("Resolvendo drive por serial='%s' ...", serial_norm)
        dev = (
            self._resolve_by_tape_by_id(serial_norm)
            or self._resolve_by_lsscsi(serial_norm)
            or self._resolve_by_sg_scan(serial_norm)
            or self._resolve_by_mhvtl(serial_norm)
        )
        if dev is None:
            raise DriveNotFoundError(
                f"Nenhum drive SCSI com serial '{serial_norm}' encontrado. "
                f"Verifique /dev/tape/by-id/, lsscsi e /etc/mhvtl/device.conf."
            )

        self.log.info(
            "Drive localizado: serial=%s st=%s nst=%s sg=%s drive#=%d",
            dev.serial, dev.st_device, dev.nst_device, dev.sg_device, dev.drive_number,
        )
        self._cache[serial_norm] = dev
        return dev

    # -----------------------------------------------------------------
    # Estratégia 1: /dev/tape/by-id/
    # -----------------------------------------------------------------
    def _resolve_by_tape_by_id(self, serial: str) -> DriveDevice | None:
        by_id = Path("/dev/tape/by-id")
        if not by_id.is_dir():
            return None
        for entry in by_id.iterdir():
            name = entry.name.upper()
            if serial in name:
                try:
                    target = entry.resolve()
                except OSError:
                    continue
                st_path = str(target)
                if not st_path.startswith("/dev/st"):
                    continue
                drive_num = _extract_drive_number(st_path)
                nst_path = f"/dev/nst{drive_num}"
                if not Path(nst_path).is_char_device():
                    continue
                self.log.debug("Match via /dev/tape/by-id: %s", entry)
                return DriveDevice(
                    serial=serial,
                    st_device=st_path,
                    nst_device=nst_path,
                    drive_number=drive_num,
                )
        return None

    # -----------------------------------------------------------------
    # Estratégia 2: lsscsi -t
    # -----------------------------------------------------------------
    def _resolve_by_lsscsi(self, serial: str) -> DriveDevice | None:
        lsscsi = self.cfg.drives.lsscsi_bin
        if not _which(lsscsi):
            return None
        try:
            out = _run([lsscsi, "-t", "-g", "-L"], timeout=30)
        except subprocess.SubprocessError as exc:
            self.log.warning("lsscsi falhou: %s", exc)
            return None

        blocks = re.split(r"\n\[\d+", out)
        current_block = ""
        for line in out.splitlines():
            if "tape" in line.lower() or "Sequential-Access" in line:
                current_block += line + "\n"
            if serial in line.upper():
                st_match = re.search(r"/dev/st(\d+)", current_block)
                sg_match = re.search(r"/dev/sg(\d+)", current_block)
                if st_match:
                    drive_num = int(st_match.group(1))
                    return DriveDevice(
                        serial=serial,
                        st_device=f"/dev/st{drive_num}",
                        nst_device=f"/dev/nst{drive_num}",
                        sg_device=(
                            f"/dev/sg{sg_match.group(1)}" if sg_match else None
                        ),
                        drive_number=drive_num,
                    )
        return None

    # -----------------------------------------------------------------
    # Estratégia 4: mhvtl (parser de /etc/mhvtl/device.conf)
    # -----------------------------------------------------------------
    def _resolve_by_mhvtl(self, serial: str) -> DriveDevice | None:
        """Estratégia mhvtl: lê /etc/mhvtl/device.conf e correlaciona."""
        if not self.cfg.mhvtl.enabled:
            return None
        try:
            from . import mhvtl as mhvtl_mod
        except ImportError:
            return None

        mcfg = mhvtl_mod.load_mhvtl(self.cfg.mhvtl.config_dir)
        if mcfg is None:
            self.log.debug("mhvtl: nenhum device.conf encontrado.")
            return None

        if self.cfg.mhvtl.auto_correlate:
            mcfg = mhvtl_mod.correlate_drives(mcfg)

        for drv in mcfg.drives:
            if not drv.nst_device:
                continue
            drv_serial = (drv.serial or "").upper()
            if serial == drv_serial or serial in drv_serial or drv_serial in serial:
                self.log.info(
                    "Match via mhvtl device.conf: Drive %d (serial=%s) -> %s",
                    drv.index, drv.serial, drv.nst_device,
                )
                return DriveDevice(
                    serial=drv.serial or serial,
                    st_device=drv.st_device or "",
                    nst_device=drv.nst_device,
                    sg_device=drv.sg_device,
                    drive_number=drv.drive_number or 0,
                )

        if self.cfg.mhvtl.fallback_to_serial:
            for drv in mcfg.drives:
                if not drv.nst_device:
                    continue
                id_serial = (drv.id_serial or "").upper()
                if id_serial and (serial == id_serial or serial in id_serial):
                    self.log.info(
                        "Match via mhvtl NAA->ID_SERIAL: Drive %d -> %s",
                        drv.index, drv.nst_device,
                    )
                    return DriveDevice(
                        serial=drv.serial or serial,
                        st_device=drv.st_device or "",
                        nst_device=drv.nst_device,
                        sg_device=drv.sg_device,
                        drive_number=drv.drive_number or 0,
                    )
        return None

    # -----------------------------------------------------------------
    # Estratégia 3: scan de /dev/sg* com sg_inq -s
    # -----------------------------------------------------------------
    def _resolve_by_sg_scan(self, serial: str) -> DriveDevice | None:
        sg_inq = self.cfg.drives.sg_inq_bin
        if not _which(sg_inq):
            return None

        sg_devices = sorted(Path("/dev").glob("sg*"),
                            key=lambda p: _natural_key(p.name))
        for sg in sg_devices:
            sg_path = str(sg)
            if not Path(sg_path).is_char_device():
                continue
            try:
                out = _run([sg_inq, "-s", sg_path], timeout=15)
            except subprocess.SubprocessError:
                continue
            m = re.search(r"Unit serial number:\s*(\S+)", out, re.IGNORECASE)
            if not m:
                continue
            found_serial = m.group(1).strip().upper()
            if serial in found_serial or found_serial in serial:
                drive_num = self._sg_to_drive_number(sg_path)
                if drive_num is None:
                    continue
                return DriveDevice(
                    serial=serial,
                    st_device=f"/dev/st{drive_num}",
                    nst_device=f"/dev/nst{drive_num}",
                    sg_device=sg_path,
                    drive_number=drive_num,
                )
        return None

    def _sg_to_drive_number(self, sg_path: str) -> int | None:
        """Correlaciona /dev/sgN -> número lógico do drive via /sys."""
        try:
            sg_major_minor = _get_device_numbers(sg_path)
        except OSError:
            return None

        scsi_tape = Path("/sys/class/scsi_tape")
        if not scsi_tape.is_dir():
            return None
        for nst_entry in scsi_tape.iterdir():
            if not nst_entry.name.startswith("nst"):
                continue
            dev_link = nst_entry / "device" / "generic"
            if dev_link.exists():
                try:
                    linked = dev_link.resolve()
                    if linked.name == Path(sg_path).name:
                        return int(nst_entry.name.removeprefix("nst"))
                except OSError:
                    pass

            dev_file = nst_entry / "dev"
            if dev_file.exists():
                try:
                    mm = dev_file.read_text().strip().replace(":", "")
                    sg_sys = Path("/sys/class/scsi_generic") / Path(sg_path).name / "dev"
                    if sg_sys.exists() and sg_sys.read_text().strip().replace(":", "") == mm:
                        return int(nst_entry.name.removeprefix("nst"))
                except OSError:
                    continue
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _which(cmd: str) -> bool:
    """Verifica se o comando existe no PATH."""
    from shutil import which
    return which(cmd) is not None


def _run(cmd: list[str], timeout: int = 30) -> str:
    """Executa e devolve stdout. Erros virão como string vazia/exceção."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.stdout or ""


def _extract_drive_number(path: str) -> int:
    m = re.search(r"(\d+)$", path)
    return int(m.group(1)) if m else 0


def _natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _get_device_numbers(path: str) -> str:
    st = os.stat(path)
    return f"{os.major(st.st_rdev)}{os.minor(st.st_rdev)}"
