"""Configuração centralizada.

Carrega o YAML em um dataclass tipado e expõe defaults sensatos.
Qualquer chave ausente no YAML recebe um valor padrão, garantindo que o
sistema nunca quebre por configuração incompleta.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigError


# ---------------------------------------------------------------------------
# Defaults de capacidades (preservados do script original)
# ---------------------------------------------------------------------------
_DEFAULT_CARTRIDGES: dict[str, dict[str, int | None]] = {
    "dummy": {"tar": 5, "ltfs": 5},
    "JRT01": {"tar": 2, "ltfs": 2},       # Cartucho de teste pequeno (2 GiB)
    "JAE05": {"tar": 460, "ltfs": None},
    "JAE06": {"tar": 590, "ltfs": None},
    "JBE05": {"tar": 650, "ltfs": None},
    "JBE06": {"tar": 925, "ltfs": None},
    "JBE07": {"tar": 1485, "ltfs": 1350},
    "JCE07": {"tar": 3720, "ltfs": 3390},
}

GIB = 1024 ** 3


@dataclass
class ChangerConfig:
    device: str | None = None
    mtx_bin: str = "mtx"
    inquiry_timeout_sec: int = 15
    status_timeout_sec: int = 60
    inventory_timeout_sec: int = 120
    load_timeout_sec: int = 180
    unload_timeout_sec: int = 180


@dataclass
class DrivesConfig:
    dedicated_serial: str | None = None
    mt_bin: str = "mt"
    tar_bin: str = "tar"
    sg_inq_bin: str = "sg_inq"
    lsscsi_bin: str = "lsscsi"
    status_retry_count: int = 5
    status_retry_delay_sec: int = 10
    online_check_timeout_sec: int = 90
    skip_online_check: bool = False   # bypass para mhvtl problemático


@dataclass
class PlannerConfig:
    algorithm: str = "ffd"
    safety_margin_bytes: int = 524_288_000        # 500 MiB
    small_file_threshold_bytes: int = 1_048_576  # 1 MiB
    plan_filename_template: str = "plan.{cartridge}.{format}.txt"
    volume_filename_template: str = "vol{index:05d}_{used_gib:05d}GiB.lst"
    # Se true, executa mt erase antes de tar -cvf. DESATIVADO no tape_manager
    # porque o mt antes de tar -cvf causa "Device busy".
    erase_before_write: bool = False


@dataclass
class WriterConfig:
    use_non_rewind_device: bool = True
    tar_extra_args: list[str] = field(default_factory=lambda: ["--no-unquote"])
    # Pós-validação NÃO FATAL no tape_manager (tape está no fim após gravação).
    post_verify: bool = True
    post_verify_timeout_sec: int = 7200
    # NÃO usar mt offline (eject) antes do unload no tape_manager.
    eject_before_unload: bool = False
    use_tar_checkpoint: bool = False
    tar_checkpoint_action: str = ""   # "" | "stderr" | "exec=..."
    # Retry do tar -cvf quando encontrar "Device busy".
    busy_retry_count: int = 3
    busy_retry_delay_sec: int = 10


@dataclass
class AutoChangeConfig:
    enabled: bool = True
    operator_prompt_timeout_sec: int = 0
    poll_interval_sec: int = 30
    max_poll_attempts: int = 0


@dataclass
class IntegrityConfig:
    progress_filename: str = "progress.json"
    pre_copy_verify: bool = True
    # post_copy_verify: a pós-validação é NÃO FATAL no tape_manager.
    post_copy_verify: bool = False
    checksum_files: bool = False
    checksum_algorithm: str = "sha256"


@dataclass
class ExtractorConfig:
    """Configuração do extrator de tapes (leitura via dd)."""
    # Tamanho de bloco para dd na leitura de dados.
    # dd if=/dev/nstN of=<file> bs=2M
    dd_block_size: str = "2M"
    # Timeout para cada operação dd (leitura de um arquivo da tape).
    dd_timeout_sec: int = 14400


@dataclass
class LoggingConfig:
    level: str = "INFO"
    console: bool = True
    file: bool = True
    file_path: str = "./logs/tape_manager.log"
    mtx_operations_log: str = "./logs/mtx_operations.log"
    max_size_mb: int = 50
    backup_count: int = 5


@dataclass
class MhvtlSettings:
    """Configuração do suporte a mhvtl (Virtual Tape Library)."""
    enabled: bool = True
    config_dir: str | None = None     # None = auto (/etc/mhvtl)
    auto_correlate: bool = True
    fallback_to_serial: bool = True


@dataclass
class Config:
    project_name: str = "Tape Manager"
    project_version: str = "2.0.0"
    default_output_dir: str = "./output"
    changer: ChangerConfig = field(default_factory=ChangerConfig)
    drives: DrivesConfig = field(default_factory=DrivesConfig)
    cartridges: dict[str, dict[str, int | None]] = field(
        default_factory=lambda: {k: dict(v) for k, v in _DEFAULT_CARTRIDGES.items()}
    )
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
    auto_change: AutoChangeConfig = field(default_factory=AutoChangeConfig)
    integrity: IntegrityConfig = field(default_factory=IntegrityConfig)
    extractor: ExtractorConfig = field(default_factory=ExtractorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    mhvtl: MhvtlSettings = field(default_factory=MhvtlSettings)
    # Flag runtime: habilita modo mhvtl (passado via CLI --mhvtl)
    # Quando False: esconde cartuchos virtuais (JRT01, dummy) e não usa
    # correlação mhvtl — apenas SCSI host (drives físicos reais).
    mhvtl_mode: bool = False

    # -----------------------------------------------------------------
    # Construtores
    # -----------------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | os.PathLike, mhvtl_mode: bool = False) -> "Config":
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"Arquivo de configuração não encontrado: {path}")
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"YAML inválido em {path}: {exc}") from exc
        config_dir = path.parent
        if config_dir.name == "config":
            base_dir = config_dir.parent
        else:
            base_dir = config_dir
        return cls.from_dict(data, base_dir=base_dir, mhvtl_mode=mhvtl_mode)

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: Path | None = None,
                  mhvtl_mode: bool = False) -> "Config":
        cfg = cls()
        cfg.mhvtl_mode = mhvtl_mode
        if not isinstance(data, dict):
            raise ConfigError("Configuração deve ser um mapping YAML.")

        proj = data.get("project", {}) or {}
        cfg.project_name = proj.get("name", cfg.project_name)
        cfg.project_version = proj.get("version", cfg.project_version)
        cfg.default_output_dir = proj.get("default_output_dir", cfg.default_output_dir)

        # Resolve default_output_dir relativo ao config.yaml (não ao CWD).
        if base_dir and cfg.default_output_dir:
            p = Path(cfg.default_output_dir)
            if not p.is_absolute():
                cfg.default_output_dir = str((base_dir / p).resolve())

        _merge_dataclass(cfg.changer, data.get("changer", {}))
        _merge_dataclass(cfg.drives, data.get("drives", {}))
        _merge_dataclass(cfg.planner, data.get("planner", {}))
        _merge_dataclass(cfg.writer, data.get("writer", {}))
        _merge_dataclass(cfg.auto_change, data.get("auto_change", {}))
        _merge_dataclass(cfg.integrity, data.get("integrity", {}))
        _merge_dataclass(cfg.extractor, data.get("extractor", {}))
        _merge_dataclass(cfg.logging, data.get("logging", {}))
        _merge_dataclass(cfg.mhvtl, data.get("mhvtl", {}))

        # Resolve paths de logging relativos ao config.yaml.
        if base_dir:
            for attr in ("file_path", "mtx_operations_log"):
                v = getattr(cfg.logging, attr, None)
                if v:
                    p = Path(v)
                    if not p.is_absolute():
                        setattr(cfg.logging, attr, str((base_dir / p).resolve()))

        carts = data.get("cartridges")
        if isinstance(carts, dict) and carts:
            cfg.cartridges = carts

        # Modo físico (sem --mhvtl): remove cartuchos virtuais (JRT01, dummy)
        # Estes são artefatos do mhvtl para testes e não existem em hardware real.
        if not cfg.mhvtl_mode:
            virtual_cartridges = {"JRT01", "dummy"}
            cfg.cartridges = {
                k: v for k, v in cfg.cartridges.items()
                if k not in virtual_cartridges
            }
            if not cfg.cartridges:
                raise ConfigError(
                    "Nenhum cartucho disponível no modo físico. "
                    "Execute com --mhvtl para usar cartuchos virtuais."
                )

        cfg.validate()
        return cfg

    # -----------------------------------------------------------------
    # Validação
    # -----------------------------------------------------------------
    def validate(self) -> None:
        if not self.cartridges:
            raise ConfigError("Tabela de cartuchos vazia.")
        for name, caps in self.cartridges.items():
            if not isinstance(caps, dict):
                raise ConfigError(f"Cartucho '{name}' deve ter sub-chaves tar/ltfs.")
        if self.auto_change.poll_interval_sec < 5:
            raise ConfigError("poll_interval_sec deve ser >= 5 segundos.")
        if self.drives.status_retry_count < 0:
            raise ConfigError("status_retry_count não pode ser negativo.")

    # -----------------------------------------------------------------
    # Helpers de domínio
    # -----------------------------------------------------------------
    def cartridge_capacity_bytes(self, cartridge: str, fmt: str) -> int:
        """Retorna capacidade real em bytes para (cartucho, formato).

        Busca case-insensitive para tolerar 'dummy', 'DUMMY', 'JBE07', etc.
        """
        fmt = fmt.lower()
        matched_key = next(
            (k for k in self.cartridges if k.lower() == cartridge.lower()),
            None,
        )
        if matched_key is None:
            raise ConfigError(
                f"Tipo de cartucho '{cartridge}' não suportado. "
                f"Disponíveis: {', '.join(sorted(self.cartridges))}"
            )
        caps = self.cartridges[matched_key]
        gib = caps.get(fmt)
        if gib is None:
            raise ConfigError(
                f"Formato '{fmt}' não suportado para o cartucho '{cartridge}'. "
                f"Disponíveis: {[k for k, v in caps.items() if v is not None]}"
            )
        return int(gib) * GIB

    def supported_cartridges(self, fmt: str | None = None) -> list[str]:
        if fmt is None:
            return sorted(self.cartridges)
        fmt = fmt.lower()
        return sorted([c for c, caps in self.cartridges.items()
                       if caps.get(fmt) is not None])

    def format_cartridge_list(self, fmt: str | None = None) -> str:
        """Retorna lista formatada de cartuchos com capacidades em GiB."""
        lines: list[str] = []
        for name in sorted(self.cartridges):
            caps = self.cartridges[name]
            if fmt:
                gib = caps.get(fmt.lower())
                if gib is None:
                    continue
                lines.append(f"  {name:<8} {fmt}: {gib:>5} GiB")
            else:
                parts = []
                for f in ("tar", "ltfs"):
                    v = caps.get(f)
                    if v is not None:
                        parts.append(f"{f}: {v} GiB")
                if parts:
                    lines.append(f"  {name:<8} " + "   ".join(parts))
        return "\n".join(lines) if lines else "  (nenhum cartucho)"


def _merge_dataclass(instance: Any, data: dict[str, Any]) -> None:
    """Copia apenas chaves conhecidas do dataclass a partir de `data`."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if hasattr(instance, key):
            setattr(instance, key, value)


# Caminho padrão do config distribuído com o projeto
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
