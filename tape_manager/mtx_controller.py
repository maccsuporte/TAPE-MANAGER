"""Controlador de Tape Library via MTX.

Documentação de referência: https://linux.die.net/man/1/mtx

Operações suportadas:
  - discover_changer()  : localiza /dev/sgX do mediumx automaticamente
  - inquiry()            : mtx inquiry
  - inventory()          : mtx inventory
  - status()             : mtx status (parsed em estrutura)
  - find_slot_by_tag()   : localiza slot que contém um VolumeTag
  - load(slot, drive)    : carrega tape slot->drive
  - unload(slot, drive)  : devolve tape drive->slot
  - first_free_slot()    : próximo slot de armazenamento vazio
  - first_loaded_slot()  : próximo slot com tape disponível para load

THREAD-SAFE: todas as operações mtx são serializadas via _mtx_lock,
permitindo execução multi-drive paralela (cada thread opera em um
/dev/nst* diferente, mas o changer físico é um recurso compartilhado).

COOLDOWN E RETRY (apenas para load/unload):
  - Após cada MOVE MEDIUM, aguarda 1s antes de liberar o lock (picker
    precisa estabilizar — especialmente em mhvtl virtual).
  - Em caso de Hardware Error (sense 04/03 = "Manual intervention
    required" / picker busy), faz até 3 retries com 5s de espera.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .exceptions import (
    ChangerCommunicationError,
    ChangerDiscoveryError,
    TapeLoadError,
    TapeUnloadError,
)
from .logger import get_logger, get_mtx_logger


# ---------------------------------------------------------------------------
# Estruturas de saída do `mtx status`
# ---------------------------------------------------------------------------
@dataclass
class StorageElement:
    slot: int
    full: bool
    volume_tag: str | None = None


@dataclass
class DriveElement:
    drive: int
    status: str               # "Empty" | "Full"
    volume_tag: str | None = None
    loaded_slot: int | None = None


@dataclass
class ChangerStatus:
    storage_elements: list[StorageElement] = field(default_factory=list)
    drives: list[DriveElement] = field(default_factory=list)
    data_transfer: list[dict] = field(default_factory=list)
    raw_output: str = ""

    def find_slot(self, volser: str) -> int | None:
        for se in self.storage_elements:
            if se.full and se.volume_tag and se.volume_tag.upper() == volser.upper():
                return se.slot
        return None

    def first_free_slot(self) -> int | None:
        for se in self.storage_elements:
            if not se.full:
                return se.slot
        return None

    def first_available_tape(self) -> tuple[int, str] | None:
        """Primeiro slot com tape disponível para carregar."""
        for se in self.storage_elements:
            if se.full:
                return (se.slot, se.volume_tag or f"SLOT{se.slot:03d}")
        return None

    def find_drive_full(self) -> DriveElement | None:
        """Retorna o primeiro drive (DTE) com tape carregada, ou None."""
        for d in self.drives:
            if d.status == "Full":
                return d
        return None

    def find_drive_by_index(self, dte_index: int) -> DriveElement | None:
        """Retorna o drive (DTE) com o índice mtx especificado, ou None."""
        for d in self.drives:
            if d.drive == dte_index:
                return d
        return None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class MTXController:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()
        self.mtx_log = get_mtx_logger()
        self._changer: str | None = None
        self._mtx_lock = threading.Lock()
        self._discover_lock = threading.Lock()

    # -----------------------------------------------------------------
    # Discover
    # -----------------------------------------------------------------
    def discover_changer(self) -> str:
        """Localiza o device changer."""
        with self._discover_lock:
            if self._changer is not None:
                return self._changer

            if self.cfg.changer.device:
                if not Path(self.cfg.changer.device).is_char_device():
                    raise ChangerDiscoveryError(
                        f"Changer configurado não é char device: {self.cfg.changer.device}"
                    )
                self._changer = self.cfg.changer.device
                self.log.info("Changer (config): %s", self._changer)
                return self._changer

            # 1. /dev/changer
            if Path("/dev/changer").is_char_device():
                self._changer = "/dev/changer"
                self.log.info("Changer encontrado: /dev/changer")
                return self._changer

            # 2. lsscsi -g
            lsscsi = self.cfg.drives.lsscsi_bin
            if _which(lsscsi):
                try:
                    proc = subprocess.run(
                        [lsscsi, "-g"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=30, check=False,
                    )
                    for line in proc.stdout.splitlines():
                        if "mediumx" in line.lower():
                            m = re.search(r"(/dev/sg\d+)", line)
                            if m and Path(m.group(1)).is_char_device():
                                self._changer = m.group(1)
                                self.log.info("Changer via lsscsi: %s", self._changer)
                                return self._changer
                except subprocess.SubprocessError as exc:
                    self.log.warning("lsscsi falhou: %s", exc)

            # 3. scan /dev/sg* com mtx inquiry
            for sg in sorted(Path("/dev").glob("sg*"), key=_natural_key):
                if not sg.is_char_device():
                    continue
                try:
                    proc = subprocess.run(
                        [self.cfg.changer.mtx_bin, "-f", str(sg), "inquiry"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, timeout=self.cfg.changer.inquiry_timeout_sec,
                        check=False,
                    )
                    if proc.returncode == 0 and "Medium Changer" in (proc.stdout or ""):
                        self._changer = str(sg)
                        self.log.info("Changer via scan mtx inquiry: %s", self._changer)
                        return self._changer
                except subprocess.SubprocessError:
                    continue

            raise ChangerDiscoveryError(
                "Não foi possível descobrir o device changer automaticamente. "
                "Defina changer.device manualmente no config.yaml."
            )

    @property
    def changer(self) -> str:
        if self._changer is None:
            self.discover_changer()
        assert self._changer is not None
        return self._changer

    # -----------------------------------------------------------------
    # Helpers de execução (THREAD-SAFE)
    # -----------------------------------------------------------------
    def _run_mtx(self, args: list[str], timeout: int,
                 retry_on_hw_error: bool = True) -> subprocess.CompletedProcess:
        """Executa mtx com lock para serializar acesso ao changer físico.

        Para operações MOVE MEDIUM (load/unload), adiciona:
          1. Delay de 1s antes de liberar o lock (cooldown do picker)
          2. Retry automático em caso de Hardware Error (sense 04/03)
             — comum em mhvtl quando o picker está "warming up"
        """
        cmd = [self.cfg.changer.mtx_bin, "-f", self.changer, *args]
        self.log.debug("Executando: %s", " ".join(cmd))

        is_move_medium = args and args[0] in ("load", "unload")
        max_retries = 3 if (is_move_medium and retry_on_hw_error) else 1

        with self._mtx_lock:
            for attempt in range(1, max_retries + 1):
                try:
                    proc = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ChangerCommunicationError(
                        f"Timeout ({timeout}s) executando {' '.join(args)}"
                    ) from exc

                # Sucesso: break
                if proc.returncode == 0:
                    break

                # Falha: detecta Hardware Error (sense 04/03) e tenta retry
                stderr = proc.stderr or ""
                if (is_move_medium and attempt < max_retries
                        and "Hardware Error" in stderr
                        and "MOVE MEDIUM" in stderr):
                    self.log.warning(
                        "mtx %s falhou com Hardware Error (tentativa %d/%d). "
                        "Aguardando 5s para picker cooldown antes de retry...",
                        args[0], attempt, max_retries,
                    )
                    time.sleep(5)
                    continue

                # Outro tipo de falha: break sem retry
                break

            # Cooldown pós-operação MOVE MEDIUM: 1s para picker estabilizar
            if is_move_medium:
                time.sleep(1)
        return proc

    def _log_mtx(self, operation: str, slot: str, drive: str,
                 status: str, rc: int) -> None:
        self.mtx_log.info(
            "%s SLOT %s -> DRIVE %s - %s (RC=%d)",
            operation, slot, drive, status, rc,
        )

    # -----------------------------------------------------------------
    # Comandos MTX
    # -----------------------------------------------------------------
    def inquiry(self) -> str:
        proc = self._run_mtx(["inquiry"], self.cfg.changer.inquiry_timeout_sec)
        if proc.returncode != 0:
            self._log_mtx("INQUIRY", "-", "-", "FALHA", proc.returncode)
            raise ChangerCommunicationError(
                f"mtx inquiry falhou (RC={proc.returncode}): {proc.stderr.strip()}"
            )
        self._log_mtx("INQUIRY", "-", "-", "OK", 0)
        return proc.stdout

    def inventory(self) -> None:
        proc = self._run_mtx(["inventory"], self.cfg.changer.inventory_timeout_sec)
        status = "OK" if proc.returncode == 0 else "FALHA"
        self._log_mtx("INVENTORY", "-", "-", status, proc.returncode)
        if proc.returncode != 0:
            raise ChangerCommunicationError(
                f"mtx inventory falhou (RC={proc.returncode}): {proc.stderr.strip()}"
            )
        self.log.info("Inventário da biblioteca executado com sucesso.")

    def status(self) -> ChangerStatus:
        proc = self._run_mtx(["status"], self.cfg.changer.status_timeout_sec)
        if proc.returncode != 0:
            self._log_mtx("STATUS", "-", "-", "FALHA", proc.returncode)
            raise ChangerCommunicationError(
                f"mtx status falhou (RC={proc.returncode}): {proc.stderr.strip()}"
            )
        self._log_mtx("STATUS", "-", "-", "OK", 0)
        parsed = self._parse_status(proc.stdout)
        parsed.raw_output = proc.stdout
        return parsed

    def load(self, slot: int, drive: int) -> None:
        proc = self._run_mtx(
            ["load", str(slot), str(drive)],
            self.cfg.changer.load_timeout_sec,
        )
        status = "OK" if proc.returncode == 0 else "FALHA"
        self._log_mtx("LOAD", str(slot), str(drive), status, proc.returncode)
        if proc.returncode != 0:
            raise TapeLoadError(
                f"mtx load {slot} {drive} falhou (RC={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        self.log.info("LOAD OK: slot %d -> drive %d", slot, drive)

    def unload(self, slot: int, drive: int) -> None:
        proc = self._run_mtx(
            ["unload", str(slot), str(drive)],
            self.cfg.changer.unload_timeout_sec,
        )
        status = "OK" if proc.returncode == 0 else "FALHA"
        self._log_mtx("UNLOAD", str(slot), str(drive), status, proc.returncode)
        if proc.returncode != 0:
            raise TapeUnloadError(
                f"mtx unload {slot} {drive} falhou (RC={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        self.log.info("UNLOAD OK: drive %d -> slot %d", drive, slot)

    def first_free_slot(self) -> int | None:
        return self.status().first_free_slot()

    def find_slot_by_tag(self, volser: str) -> int | None:
        return self.status().find_slot(volser)

    def first_available_tape(self) -> tuple[int, str] | None:
        return self.status().first_available_tape()

    # -----------------------------------------------------------------
    # Correlação /dev/nst* <-> Data Transfer Element mtx
    # -----------------------------------------------------------------
    def _changer_scsi_host(self) -> int | None:
        """Retorna o número do SCSI host (HBA) do changer."""
        try:
            sg_name = Path(self.changer).name
            link = Path("/sys/class/scsi_generic") / sg_name / "device"
            if not link.exists():
                return None
            current = link.resolve()
            for _ in range(10):
                name = current.name
                if name.startswith("host") and name[4:].isdigit():
                    return int(name[4:])
                if current.parent == current:
                    break
                current = current.parent
        except OSError:
            pass
        return None

    def _list_tape_drives_on_host(self, host: int) -> list[tuple[int, str, str]]:
        """Lista drives de fita em um SCSI host."""
        result: list[tuple[tuple[int, int, int, int], str, str]] = []
        scsi_tape = Path("/sys/class/scsi_tape")
        if not scsi_tape.is_dir():
            return []

        for nst_entry in scsi_tape.iterdir():
            if not nst_entry.name.startswith("nst"):
                continue
            dev_dir = nst_entry / "device"
            if not dev_dir.is_dir():
                continue
            try:
                ch = int((dev_dir / "channel").read_text().strip())
                tgt = int((dev_dir / "id").read_text().strip())
                lun = int((dev_dir / "lun").read_text().strip())
                host_link = dev_dir.resolve()
                host_name = None
                current = host_link
                for _ in range(10):
                    if current.name.startswith("host") and current.name[4:].isdigit():
                        host_name = int(current.name[4:])
                        break
                    if current.parent == current:
                        break
                    current = current.parent
                if host_name != host:
                    continue

                num = nst_entry.name.removeprefix("nst")
                nst_dev = f"/dev/nst{num}"
                st_dev = f"/dev/st{num}"
                sg_dev = None
                gen = dev_dir / "generic"
                if gen.exists():
                    try:
                        sg_dev = f"/dev/{gen.resolve().name}"
                    except OSError:
                        pass
                result.append(((host, ch, tgt, lun), nst_dev, sg_dev))
            except (OSError, ValueError):
                continue

        result.sort(key=lambda x: x[0])
        return [(nst, sg) for _, nst, sg in result]

    def find_dte_for_drive(self, nst_device: str) -> int | None:
        """Descobre o índice do Data Transfer Element (mtx) para um /dev/nst*.

        Estratégia (em ordem):
          1. Correlação via SCSI host (sysfs) — funciona para hardware real.
          2. Correlação via mhvtl device.conf + WWN/serial — para mhvtl.
          3. Heurística single-drive: 1 DTE mtx + device existe.

        Retorna None se TODAS as estratégias falharem. NÃO faz fallback
        silencioso para DTE 0 quando há múltiplos /dev/nst* (causaria
        colisão entre threads paralelas).
        """
        try:
            status = self.status()
        except Exception as exc:
            self.log.warning("Falha ao obter mtx status para DTE lookup: %s", exc)
            return None

        if not status.drives:
            self.log.error("mtx status não reporta nenhum Data Transfer Element.")
            return None

        # ---- Estratégia 1: SCSI host correlation (sysfs) ----
        dte = self._find_dte_via_scsi_host(nst_device, status)
        if dte is not None:
            return dte

        # ---- Estratégia 2: mhvtl correlation (device.conf + WWN/serial) ----
        # Só tenta se o modo mhvtl estiver ativado (cfg.mhvtl_mode = True).
        # Sem --mhvtl, operamos apenas com drives físicos reais.
        if getattr(self.cfg, 'mhvtl_mode', False):
            dte = self._find_dte_via_mhvtl(nst_device, status)
            if dte is not None:
                return dte
        else:
            self.log.debug(
                "Modo mhvtl desativado — pulando correlação mhvtl para %s",
                nst_device,
            )

        # ---- Estratégia 3: heurística single-drive ----
        # Só é seguro se houver EXATAMENTE 1 DTE e o /dev/nst* existir.
        # Se houver múltiplos /dev/nst*, retorna None para forçar
        # a consolidação em single-drive no Executor.
        if len(status.drives) == 1:
            all_nst = sorted(Path("/dev").glob("nst*"))
            # Filtra apenas base devices (sem variantes l/m/a)
            all_nst = [p for p in all_nst
                       if p.is_char_device() and p.name.removeprefix("nst").isdigit()]
            if len(all_nst) == 1:
                dte_idx = status.drives[0].drive
                self.log.info(
                    "Heurística single-drive: 1 DTE mtx + 1 /dev/nst* base. "
                    "DTE mtx=%d (Linux %s)", dte_idx, nst_device,
                )
                return dte_idx
            self.log.warning(
                "1 DTE mtx mas %d /dev/nst* base devices no sistema. "
                "Correlação ambígua — deixa Executor consolidar em single-drive.",
                len(all_nst),
            )
            # Se o device pedido for o primeiro base device, retorna DTE 0
            # (consolidação single-drive vai usá-lo).
            if all_nst and nst_device == str(all_nst[0]):
                dte_idx = status.drives[0].drive
                self.log.info(
                    "Heurística: %s é o primeiro base device. DTE mtx=%d.",
                    nst_device, dte_idx,
                )
                return dte_idx
            return None

        self.log.error(
            "Não foi possível correlacionar %s com nenhum DTE mtx via "
            "SCSI host, mhvtl, ou heurística single-drive.",
            nst_device,
        )
        return None

    def _find_dte_via_scsi_host(self, nst_device: str, status) -> int | None:
        """Estratégia 1: correlação via SCSI host (sysfs)."""
        host = self._changer_scsi_host()
        if host is None:
            return None

        drives_on_host = self._list_tape_drives_on_host(host)
        self.log.info(
            "Drives de fita no host %d do changer (ordenados por HCTL):",
            host,
        )
        for i, (nst, sg) in enumerate(drives_on_host):
            self.log.info("  DTE %d: %s (sg=%s)", i, nst, sg)

        for i, (nst, _sg) in enumerate(drives_on_host):
            if nst == nst_device:
                if i < len(status.drives):
                    dte_idx = status.drives[i].drive
                    self.log.info(
                        "Match (SCSI host): %s = DTE mtx %d (posição %d no host %d)",
                        nst_device, dte_idx, i, host,
                    )
                    return dte_idx
                self.log.warning(
                    "Posição %d no host excede número de DTEs mtx (%d). "
                    "Usando índice direto.", i, len(status.drives),
                )
                return i

        self.log.warning(
            "%s não encontrado entre os drives do host %d (SCSI host).",
            nst_device, host,
        )
        return None

    def _find_dte_via_mhvtl(self, nst_device: str, status) -> int | None:
        """Estratégia 2: correlação via mhvtl device.conf + WWN/serial.

        IMPORTANTE: só retorna DTEs que existem no mtx status (changer real).
        Se o mhvtl tem mais drives que o changer físico controla (ex: mhvtl
        com 2 libraries mas só 1 changer /dev/sgN), os drives excedentes
        retornam None — o Executor vai consolidar em drives válidos.
        """
        try:
            from . import mhvtl as mhvtl_mod
            mcfg = mhvtl_mod.load_mhvtl()
            if mcfg is None or not mcfg.drives:
                return None
            if getattr(self.cfg.mhvtl, "auto_correlate", True):
                mcfg = mhvtl_mod.correlate_drives(mcfg)
            # Agrupa drives por library (VTL) e ordena
            libraries: dict[int, list] = {}
            for drv in mcfg.drives:
                libraries.setdefault(drv.library_id, []).append(drv)
            # Número de DTEs que o changer físico controla
            num_dtes = len(status.drives)
            # Mapeia drives para DTEs na ordem: library_id crescente,
            # dentro de cada library, drive index crescente
            dte_idx = 0
            for lib_id in sorted(libraries.keys()):
                drv_list = sorted(libraries[lib_id], key=lambda d: d.index)
                for drv in drv_list:
                    if drv.nst_device == nst_device:
                        if dte_idx < num_dtes:
                            real_dte = status.drives[dte_idx].drive
                            self.log.info(
                                "Match (mhvtl): %s = DTE mtx %d (library %d, drive %d)",
                                nst_device, real_dte, lib_id, drv.index,
                            )
                            return real_dte
                        # Drive mhvtl existe mas não tem DTE correspondente
                        # no changer físico (ex: library 30 com /dev/sg6 que
                        # só controla library 10). Retorna None para que o
                        # Executor consolide em um drive válido.
                        self.log.warning(
                            "Match (mhvtl): %s = library %d drive %d, mas "
                            "dte_idx=%d excede número de DTEs do changer (%d). "
                            "Este drive NÃO está acessível pelo changer %s. "
                            "Retornando None para forçar consolidação.",
                            nst_device, lib_id, drv.index,
                            dte_idx, num_dtes, self.changer,
                        )
                        return None
                    dte_idx += 1
            self.log.warning(
                "%s não encontrado na correlação mhvtl (device.conf).",
                nst_device,
            )
            return None
        except Exception as exc:
            self.log.debug("Correlação mhvtl falhou: %s", exc)
            return None


    # -----------------------------------------------------------------
    # Parser de `mtx status`
    # -----------------------------------------------------------------
    @staticmethod
    def _parse_status(output: str) -> ChangerStatus:
        status = ChangerStatus()
        lines = output.splitlines()

        merged: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if merged and (stripped.startswith("(") or
                           stripped.startswith(":VolumeTag")):
                merged[-1] = merged[-1] + " " + stripped
            else:
                merged.append(stripped)

        for line in merged:
            if not line:
                continue
            m = re.match(
                r"Storage Element (\d+):\s*(Empty|Full)"
                r"(?:\s*:?\s*VolumeTag='([^']*)')?",
                line,
            )
            if m:
                slot = int(m.group(1))
                full = m.group(2) == "Full"
                tag = m.group(3)
                status.storage_elements.append(
                    StorageElement(slot=slot, full=full, volume_tag=tag)
                )
                continue

            m = re.match(
                r"Data Transfer Element (\d+):\s*(Empty|Full)"
                r"(?:\s*:?\s*VolumeTag='([^']*)')?"
                r"(?:\s*\(Storage Element (\d+)\))?",
                line,
            )
            if m:
                drive = int(m.group(1))
                full = m.group(2) == "Full"
                tag = m.group(3)
                loaded_slot = int(m.group(4)) if m.group(4) else None
                status.drives.append(
                    DriveElement(
                        drive=drive,
                        status="Full" if full else "Empty",
                        volume_tag=tag,
                        loaded_slot=loaded_slot,
                    )
                )
                continue

            status.data_transfer.append({"raw": line})
        return status


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------
def _which(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _natural_key(s) -> list:
    name = str(s)
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]
