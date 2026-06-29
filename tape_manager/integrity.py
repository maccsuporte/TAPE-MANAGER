"""Integridade de dados e recuperação.

Implementa:
  - validação pré-cópia (cada arquivo existe, tamanho bate com o plano)
  - controle de arquivos pendentes
  - registro de progresso persistível (ProgressState) — thread-safe
  - retomada automática após falha
  - helper _rewind_tape (mt rewind FUNCIONA para leitura)
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path

from .config import Config
from .exceptions import IntegrityError
from .logger import get_logger
from .models import ProgressState, SurveyPlan, VolumePlan


class IntegrityChecker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()

    # -----------------------------------------------------------------
    # Pré-cópia
    # -----------------------------------------------------------------
    def verify_volume_pre(self, plan: SurveyPlan, vol: VolumePlan) -> None:
        """Valida todos os arquivos do volume antes de gravar."""
        if not self.cfg.integrity.pre_copy_verify:
            return
        for f in vol.files:
            full = Path(plan.basedir) / f.path
            if not full.is_file():
                raise IntegrityError(f"Arquivo não encontrado: {full}")
            try:
                actual = full.stat().st_size
            except OSError as exc:
                raise IntegrityError(
                    f"Falha ao stat {full}: {exc}"
                ) from exc
            if actual != f.size:
                raise IntegrityError(
                    f"Tamanho diverge para {full}: esperado={f.size} atual={actual}"
                )
            if self.cfg.integrity.checksum_files:
                self._checksum(full)
            f.verified_pre = True
        self.log.info("Pré-validação OK: %s (%d arquivos)", vol.volser, len(vol.files))

    # -----------------------------------------------------------------
    # Helper: mt rewind (FUNCIONA para leitura)
    # -----------------------------------------------------------------
    def _rewind_tape(self, drive_device: str) -> None:
        """Executa mt -f <drive> rewind antes de ler a tape.

        mt rewind FUNCIONA para leitura (tar -tf / dd).
        O busy só acontece quando mt precede tar -cvf (escrita).
        """
        try:
            proc = subprocess.run(
                [self.cfg.drives.mt_bin, "-f", drive_device, "rewind"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise IntegrityError(
                f"Timeout no mt rewind para {drive_device}"
            ) from exc
        if proc.returncode != 0:
            self.log.warning(
                "mt rewind retornou RC=%d para %s: %s. Tentando novamente em 5s...",
                proc.returncode, drive_device, (proc.stderr or "").strip(),
            )
            import time as _t
            _t.sleep(5)
            try:
                proc = subprocess.run(
                    [self.cfg.drives.mt_bin, "-f", drive_device, "rewind"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=120, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise IntegrityError(
                    f"Timeout no mt rewind (retry) para {drive_device}"
                ) from exc
            if proc.returncode != 0:
                raise IntegrityError(
                    f"mt rewind falhou (RC={proc.returncode}) para {drive_device}: "
                    f"{(proc.stderr or '').strip()}"
                )
        self.log.info("Tape rebobinada em %s", drive_device)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def _checksum(self, path: Path) -> str:
        algo = hashlib.new(self.cfg.integrity.checksum_algorithm)
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                algo.update(chunk)
        return algo.hexdigest()


# ---------------------------------------------------------------------------
# Tracker de progresso (wrapper conveniente sobre ProgressState)
# Thread-safe para execução multi-drive paralela.
# ---------------------------------------------------------------------------
class ProgressTracker:
    def __init__(self, plan: SurveyPlan, state: ProgressState, cfg: Config):
        self.plan = plan
        self.state = state
        self.cfg = cfg
        self._path = Path(plan.output_dir) / cfg.integrity.progress_filename
        self._lock = threading.Lock()

    @classmethod
    def for_plan(cls, plan: SurveyPlan, cfg: Config) -> "ProgressTracker":
        """Localiza (ou cria) o arquivo de progresso."""
        new_path = Path(plan.output_dir) / cfg.integrity.progress_filename
        legacy_path = Path(plan.output_dir) / ".progress.json"
        if new_path.is_file():
            path = new_path
        elif legacy_path.is_file():
            path = new_path
            try:
                state = ProgressState.load(legacy_path)
                state.save(new_path)
                tracker = cls(plan, state, cfg)
                tracker._path = new_path
                return tracker
            except Exception:
                pass
            path = legacy_path
        else:
            path = new_path
        state = ProgressState.load_or_create(path, plan.plan_file)
        tracker = cls(plan, state, cfg)
        tracker._path = new_path  # sempre salva no novo nome
        return tracker

    def save(self) -> None:
        with self._lock:
            self.state.save(self._path)

    def mark_volume_started(self, vol: VolumePlan) -> None:
        with self._lock:
            self.state.mark_volume_started(vol.volser)
            vol.status = "writing"
            vol.started_at = vol.started_at or __import__("time").time()
            self.state.save(self._path)

    def mark_file_written(self, vol: VolumePlan, path: str) -> None:
        with self._lock:
            self.state.mark_file_written(vol.volser, path)
            self.state.save(self._path)

    def mark_volume_completed(self, vol: VolumePlan) -> None:
        with self._lock:
            self.state.mark_volume_completed(vol.volser)
            vol.status = "written"
            import time
            vol.finished_at = time.time()
            self.state.save(self._path)

    def mark_volume_failed(self, vol: VolumePlan) -> None:
        with self._lock:
            self.state.mark_volume_failed(vol.volser)
            vol.status = "failed"
            self.state.save(self._path)

    def register_tape_change(self) -> None:
        with self._lock:
            self.state.register_tape_change()
            self.state.save(self._path)

    def is_volume_done(self, vol: VolumePlan) -> bool:
        with self._lock:
            return self.state.is_volume_done(vol.volser)

    def written_files_for(self, vol: VolumePlan) -> list[str]:
        with self._lock:
            return self.state.written_files.get(vol.volser, [])
