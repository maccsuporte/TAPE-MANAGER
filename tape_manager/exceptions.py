"""Exceções customizadas do Tape Manager.

Centralizar os erros de domínio facilita o tratamento pelo orquestrador
e produz mensagens claras para o operador.
"""

from __future__ import annotations


class TapeManagerError(Exception):
    """Erro base do domínio Tape Manager."""


class ConfigError(TapeManagerError):
    """Falha ao carregar/validar configuração."""


class ChangerDiscoveryError(TapeManagerError):
    """Não foi possível localizar o device changer."""


class ChangerCommunicationError(TapeManagerError):
    """mtx não conseguiu falar com o changer."""


class DriveNotFoundError(TapeManagerError):
    """Serial number do drive não corresponde a nenhum dispositivo."""


class VolumeNotFoundError(TapeManagerError):
    """Volume Tag (volser) não encontrado no inventário."""


class NoTapeAvailableError(TapeManagerError):
    """Nenhuma tape disponível na library para continuar."""

    def __init__(self, required: int = 1, expected_slot: int | None = None,
                 volser_hint: str | None = None):
        self.required = required
        self.expected_slot = expected_slot
        self.volser_hint = volser_hint
        super().__init__(
            f"Nenhuma tape disponível. Necessárias={required}, "
            f"slot_esperado={expected_slot}, volser_desejado={volser_hint}"
        )


class TapeLoadError(TapeManagerError):
    """Falha no comando mtx load."""


class TapeUnloadError(TapeManagerError):
    """Falha no comando mtx unload."""


class TapeNotOnlineError(TapeManagerError):
    """Tape carregada mas não ficou ONLINE no drive."""


class PlanValidationError(TapeManagerError):
    """Plano inválido (formato, capacidades, arquivos inexistentes)."""


class IntegrityError(TapeManagerError):
    """Falha de integridade pré ou pós-cópia."""


class OperatorAbortError(TapeManagerError):
    """Operador interrompeu manualmente a execução."""


class WriterError(TapeManagerError):
    """Erro durante a gravação TAR."""


class ExtractorError(TapeManagerError):
    """Erro durante a extração de arquivos da tape (dd)."""
