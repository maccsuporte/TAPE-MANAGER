"""Tape Manager.

Consolida em um único projeto modular as operações de gestão de tape
library, com suporte a multi-drive paralelo, multi-cartucho e extração
via dd.

Evolução do tape_survey_manager com todas as correções acumuladas:

  - NUNCA usar mt antes de tar -cvf (evita "Device busy" na escrita)
  - mt rewind ANTES de ler (dd / tar -tf) FUNCIONA
  - EXTRAÇÃO via dd if=/dev/nstN of=<file> bs=2M em loop (não tar -xf)
  - GRAVAÇÃO via tar -cvf (mantido)
  - MULTI-DRIVE paralelo: threads independentes por /dev/nst*
  - mtx unload sem mt offline (serializado via lock para multi-drive)
  - Retry 3x/10s para Device busy no tar -cvf
  - _used_slots por drive (não reutilizar slot)
  - Volsers auto-gerados como vol001, vol002, ... (sem input de labels)
  - SEM _TAPE_LABEL.txt (não gravar label na tape)
  - Menu de 11 opções (status/inventário/drives/plano/validar/executar/
    progresso/retomar/extrair/erase/drive-status)
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = [
    "config",
    "logger",
    "exceptions",
    "models",
    "drive_resolver",
    "mtx_controller",
    "mhvtl",
    "planner",
    "tape_writer",
    "tape_extractor",
    "integrity",
    "operator_alerts",
    "executor",
]
