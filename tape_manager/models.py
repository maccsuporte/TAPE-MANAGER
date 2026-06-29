"""Modelos de domínio (dataclasses) usados pelo planner, writer, extractor e
executor.

Representam de forma tipada os conceitos do fluxo:
  - FileEntry        : arquivo encontrado no diretório survey
  - VolumePlan       : uma tape (volser) + lista de arquivos a gravar
                       (inclui campo `cartridge` para multi-cartucho)
  - SurveyPlan       : plano completo (volumes + metadados + multi-drive)
  - ExtractedFile    : arquivo extraído de uma tape
  - TapeManifest     : manifesto de uma tape (lista de arquivos)
  - ExtractionBatch  : lote de extração (múltiplas tapes -> um destdir)
  - ProgressState    : estado persistível para retomar execução
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# FileEntry
# ---------------------------------------------------------------------------
@dataclass
class FileEntry:
    """Arquivo único a ser gravado em uma tape."""
    path: str             # caminho relativo ao basedir do survey
    size: int             # bytes
    basedir: str = ""     # diretório base para o `tar -C`
    verified_pre: bool = False
    verified_post: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileEntry":
        return cls(**d)


# ---------------------------------------------------------------------------
# VolumePlan
# ---------------------------------------------------------------------------
@dataclass
class VolumePlan:
    """Uma tape: volser + arquivos + capacidade.

    O campo `cartridge` suporta planos multi-cartucho, onde cada volume
    pode ser gravado em um tipo de cartucho diferente.
    """
    index: int                       # 1-based
    volser: str                      # ex: label001
    toc_filename: str                # ex: vol00001_0459GiB.lst
    toc_path: str                    # path absoluto para o .lst
    files: list[FileEntry] = field(default_factory=list)
    used_bytes: int = 0
    max_bytes: int = 0
    status: str = "pending"          # pending | writing | written | verified | failed
    started_at: float | None = None
    finished_at: float | None = None
    # ----- Multi-cartucho -----
    # Tipo de cartucho deste volume (default: vazio = usa o cartucho do plano).
    # Quando preenchido, sobrescreve plan.cartridge para este volume.
    cartridge: str = ""

    @property
    def used_gib(self) -> int:
        return self.used_bytes // (1024 ** 3)

    @property
    def effective_cartridge(self) -> str:
        """Cartucho efetivo (volume > plano)."""
        return self.cartridge

    def add_file(self, f: FileEntry) -> None:
        self.files.append(f)
        self.used_bytes += f.size

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "volser": self.volser,
            "toc_filename": self.toc_filename,
            "toc_path": self.toc_path,
            "files": [f.to_dict() for f in self.files],
            "used_bytes": self.used_bytes,
            "max_bytes": self.max_bytes,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cartridge": self.cartridge,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VolumePlan":
        files = [FileEntry.from_dict(f) for f in d.pop("files", [])]
        import dataclasses as _dc
        known = {f.name for f in _dc.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(files=files, **filtered)


# ---------------------------------------------------------------------------
# SurveyPlan
# ---------------------------------------------------------------------------
@dataclass
class SurveyPlan:
    """Plano completo de gravação de um survey.

    MULTI-DRIVE: drive_assignments mapeia /dev/nstN -> [índices de volume].
    slot_assignments mapeia slot (str) -> índice de volume.
    """
    survey_dir: str
    basedir: str
    survey_name: str
    cartridge: str             # ex: JBE07 (cartucho padrão do plano)
    fmt: str                   # tar | ltfs
    output_dir: str            # dir onde plan + TOCs vivem
    plan_file: str             # path absoluto
    volumes: list[VolumePlan] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    skipped_files: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    # ----- MULTI-DRIVE -----
    # Mapeia /dev/nstN -> [índices de volume que este drive processa].
    # Ex: {"/dev/nst0": [1, 2], "/dev/nst1": [3]}
    drive_assignments: dict[str, list[int]] = field(default_factory=dict)
    # Mapeia slot (str) -> índice de volume.
    # Ex: {"1": 1, "2": 2, "3": 3}
    slot_assignments: dict[str, int] = field(default_factory=dict)

    # ----- Serialização -----
    def to_dict(self) -> dict[str, Any]:
        return {
            "survey_dir": self.survey_dir,
            "basedir": self.basedir,
            "survey_name": self.survey_name,
            "cartridge": self.cartridge,
            "fmt": self.fmt,
            "output_dir": self.output_dir,
            "plan_file": self.plan_file,
            "volumes": [v.to_dict() for v in self.volumes],
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "skipped_files": self.skipped_files,
            "created_at": self.created_at,
            "drive_assignments": self.drive_assignments,
            "slot_assignments": self.slot_assignments,
        }

    def to_json(self, path: str | Path) -> None:
        """Persiste o plano como JSON atômico (escreve .tmp + rename)."""
        import os as _os
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            _os.fsync(fh.fileno())
        tmp.replace(path)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SurveyPlan":
        volumes = [VolumePlan.from_dict(v) for v in d.pop("volumes", [])]
        import dataclasses as _dc
        known = {f.name for f in _dc.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(volumes=volumes, **filtered)

    @classmethod
    def from_json(cls, path: str | Path) -> "SurveyPlan":
        """Carrega plano de JSON. Lança exceção clara se corrompido."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Arquivo de plano não encontrado: {path}")
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Arquivo JSON corrompido: {path}\n"
                f"  Detalhe: {exc}\n"
                f"  Dica: recrie o plano com a opção 4 do menu."
            ) from exc
        return cls.from_dict(data)

    # ----- Helpers -----
    @property
    def pending_volumes(self) -> list[VolumePlan]:
        return [v for v in self.volumes if v.status not in ("written", "verified")]

    def find_volume(self, volser: str) -> VolumePlan | None:
        for v in self.volumes:
            if v.volser == volser:
                return v
        return None

    def slot_for_volume(self, vol_index: int) -> int | None:
        """Retorna o slot forçado para um índice de volume, ou None."""
        for slot_str, vidx in self.slot_assignments.items():
            if vidx == vol_index:
                try:
                    return int(slot_str)
                except (ValueError, TypeError):
                    return None
        return None

    def volume_for_slot(self, slot: int) -> int | None:
        """Retorna o índice de volume para um slot, ou None."""
        return self.slot_assignments.get(str(slot))


# ---------------------------------------------------------------------------
# ExtractedFile  (arquivo extraído de uma tape)
# ---------------------------------------------------------------------------
@dataclass
class ExtractedFile:
    """Arquivo extraído de uma tape durante a operação de extração (dd)."""
    path: str             # nome do arquivo no destdir (ex: volser_001.sgy)
    size: int = 0         # bytes
    volser: str = ""      # tape de origem
    slot: int = 0         # slot da tape de origem
    extracted: bool = False
    sha256: str = ""      # checksum pós-extração (opcional)
    status: str = "ok"    # ok | err

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExtractedFile":
        import dataclasses as _dc
        known = {f.name for f in _dc.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# TapeManifest  (manifesto de uma tape)
# ---------------------------------------------------------------------------
@dataclass
class TapeManifest:
    """Manifesto de uma tape: volser + lista de arquivos extraídos.

    Gerado durante a extração via dd. Persistido como JSON.
    """
    volser: str
    slot: int = 0
    cartridge: str = ""
    fmt: str = "tar"
    files: list[ExtractedFile] = field(default_factory=list)
    extracted_at: float = field(default_factory=time.time)
    output_dir: str = ""
    status: str = "ok"              # ok | partial | error
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "volser": self.volser,
            "slot": self.slot,
            "cartridge": self.cartridge,
            "fmt": self.fmt,
            "files": [f.to_dict() for f in self.files],
            "extracted_at": self.extracted_at,
            "output_dir": self.output_dir,
            "status": self.status,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TapeManifest":
        files = [ExtractedFile.from_dict(f) for f in d.get("files", [])]
        import dataclasses as _dc
        known = {f.name for f in _dc.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known and k != "files"}
        return cls(files=files, **filtered)

    def to_json(self, path: str | Path) -> None:
        import os as _os
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            _os.fsync(fh.fileno())
        tmp.replace(path)

    @classmethod
    def from_json(cls, path: str | Path) -> "TapeManifest":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Manifesto não encontrado: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# ExtractionBatch  (lote de extração: múltiplas tapes -> um destdir)
# ---------------------------------------------------------------------------
@dataclass
class ExtractionBatch:
    """Lote de extração: conjunto de tapes extraídas para um diretório."""
    batch_id: str                    # identificador único (timestamp)
    output_dir: str                  # diretório de destino
    manifests: list[TapeManifest] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"          # running | completed | partial | failed
    total_files: int = 0
    total_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    def add_manifest(self, m: TapeManifest) -> None:
        self.manifests.append(m)
        self.total_files += sum(1 for f in m.files if f.extracted)
        self.total_bytes += sum(f.size for f in m.files if f.extracted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "output_dir": self.output_dir,
            "manifests": [m.to_dict() for m in self.manifests],
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExtractionBatch":
        manifests = [TapeManifest.from_dict(m) for m in d.get("manifests", [])]
        import dataclasses as _dc
        known = {f.name for f in _dc.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known and k != "manifests"}
        return cls(manifests=manifests, **filtered)

    def to_json(self, path: str | Path) -> None:
        import os as _os
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            _os.fsync(fh.fileno())
        tmp.replace(path)

    @classmethod
    def from_json(cls, path: str | Path) -> "ExtractionBatch":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Batch não encontrado: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# ProgressState  (estado persistível para retomar)
# ---------------------------------------------------------------------------
@dataclass
class ProgressState:
    """Estado de execução associado a um SurveyPlan.

    Persistido como JSON no mesmo diretório do plano. Permite:
      - retomar do último volume concluído
      - retomar arquivo a arquivo dentro de um volume em writing
      - registrar trocas de tape
    """
    plan_file: str
    current_volume: str | None = None
    completed_volumes: list[str] = field(default_factory=list)
    failed_volumes: list[str] = field(default_factory=list)
    written_files: dict[str, list[str]] = field(default_factory=dict)  # volser -> [paths]
    tape_changes: int = 0
    last_update: float = field(default_factory=time.time)
    notes: list[str] = field(default_factory=list)

    def mark_volume_started(self, volser: str) -> None:
        self.current_volume = volser
        self.written_files.setdefault(volser, [])
        self.last_update = time.time()

    def mark_file_written(self, volser: str, path: str) -> None:
        lst = self.written_files.setdefault(volser, [])
        if path not in lst:
            lst.append(path)
        self.last_update = time.time()

    def mark_volume_completed(self, volser: str) -> None:
        if volser not in self.completed_volumes:
            self.completed_volumes.append(volser)
        if self.current_volume == volser:
            self.current_volume = None
        self.last_update = time.time()

    def mark_volume_failed(self, volser: str) -> None:
        if volser not in self.failed_volumes:
            self.failed_volumes.append(volser)
        self.last_update = time.time()

    def register_tape_change(self) -> None:
        self.tape_changes += 1
        self.last_update = time.time()

    def is_volume_done(self, volser: str) -> bool:
        return volser in self.completed_volumes

    def is_volume_partially_written(self, volser: str) -> bool:
        return bool(self.written_files.get(volser))

    # ----- Persistência -----
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
        tmp.replace(path)            # atômico

    @classmethod
    def load(cls, path: str | Path) -> "ProgressState":
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(**data)

    @classmethod
    def load_or_create(cls, path: str | Path, plan_file: str) -> "ProgressState":
        p = Path(path)
        if p.is_file():
            try:
                return cls.load(p)
            except Exception:
                pass
        return cls(plan_file=plan_file)
