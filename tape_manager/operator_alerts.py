"""Alertas ao operador.

Cenário: library sem tape disponível para continuar.
Ação:
  1. Pausa a execução
  2. Exibe mensagem clara com:
     - quantidade de tapes necessárias
     - slot esperado (se aplicável)
     - volser desejado (se conhecido)
  3. Aguarda inserção (polling de inventário)
  4. Atualiza inventário automaticamente
  5. Retorna controle ao chamador para continuar exatamente do ponto
"""

from __future__ import annotations

import time
from typing import Callable

from .config import Config
from .exceptions import OperatorAbortError
from .logger import get_logger
from .mtx_controller import MTXController


class OperatorAlerts:
    def __init__(self, cfg: Config, mtx: MTXController):
        self.cfg = cfg
        self.mtx = mtx
        self.log = get_logger()

    def request_tape(
        self,
        required: int = 1,
        expected_slot: int | None = None,
        volser_hint: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, str]:
        """Bloqueia até o operador inserir a tape necessária.

        Retorna (slot, volser) disponível para uso.
        Lança OperatorAbortError se o operador cancelar.
        """
        self.log.warning(
            "PAUSA OPERADOR: tapes_necessarias=%d slot_esperado=%s volser=%s",
            required, expected_slot, volser_hint,
        )
        print("\n" + "=" * 70)
        print("  >>> ATENÇÃO OPERADOR <<<")
        print(f"  Quantidade de tapes necessárias: {required}")
        if expected_slot is not None:
            print(f"  Slot esperado..............: {expected_slot}")
        if volser_hint:
            print(f"  Volume Tag desejado........: {volser_hint}")
        print("  Insira a(s) mídia(s) na library e pressione ENTER para")
        print("  atualizar o inventário automaticamente.")
        print("  (Digite 'cancel' para interromper a execução)")
        print("=" * 70 + "\n")

        poll = max(5, self.cfg.auto_change.poll_interval_sec)
        attempts = 0
        max_attempts = self.cfg.auto_change.max_poll_attempts

        while True:
            if is_cancelled and is_cancelled():
                raise OperatorAbortError("Operador cancelou a espera por tape.")
            # Polling: tenta refresh do inventário
            try:
                self.mtx.inventory()
                status = self.mtx.status()
            except Exception as exc:
                self.log.warning("Falha no poll do inventário: %s", exc)
                time.sleep(poll)
                attempts += 1
                if max_attempts and attempts >= max_attempts:
                    raise
                continue

            available = status.first_available_tape()
            if available is not None:
                slot, volser = available
                self.log.info(
                    "Operador inseriu tape: slot=%d volser=%s", slot, volser
                )
                print(f"\n[Tape detectada] slot={slot} volser={volser}\n")
                return slot, volser

            attempts += 1
            if max_attempts and attempts >= max_attempts:
                raise RuntimeError(
                    f"Máximo de tentativas ({max_attempts}) atingido aguardando tape."
                )
            self.log.info("Aguardando tape (tentativa %d) ...", attempts)
            time.sleep(poll)

    def request_slot_forced(
        self,
        forced_slot: int,
        volser_hint: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[int, str]:
        """Bloqueia até o operador inserir a tape no slot FORÇADO específico.

        Usado quando o plan.json tem slot_map com slots pré-determinados.
        Retorna (forced_slot, volser) quando a tape for detectada no slot.
        """
        self.log.warning(
            "PAUSA OPERADOR (slot forçado): slot=%s volser=%s",
            forced_slot, volser_hint,
        )
        print("\n" + "=" * 70)
        print("  >>> ATENÇÃO OPERADOR <<<")
        print(f"  Insira a tape no SLOT {forced_slot} da library")
        if volser_hint:
            print(f"  Volume Tag esperado........: {volser_hint}")
        print("  e pressione ENTER para atualizar o inventário.")
        print("  (Digite 'cancel' para interromper a execução)")
        print("=" * 70 + "\n")

        poll = max(5, self.cfg.auto_change.poll_interval_sec)
        attempts = 0
        max_attempts = self.cfg.auto_change.max_poll_attempts

        while True:
            if is_cancelled and is_cancelled():
                raise OperatorAbortError("Operador cancelou a espera por tape.")
            try:
                self.mtx.inventory()
                status = self.mtx.status()
            except Exception as exc:
                self.log.warning("Falha no poll do inventário: %s", exc)
                time.sleep(poll)
                attempts += 1
                if max_attempts and attempts >= max_attempts:
                    raise
                continue

            # Procura o slot forçado entre os storage elements
            for se in status.storage_elements:
                if se.slot == forced_slot and se.full:
                    volser = se.volume_tag or (volser_hint or f"SLOT{forced_slot:03d}")
                    self.log.info(
                        "Operador inseriu tape no slot forçado: slot=%d volser=%s",
                        forced_slot, volser,
                    )
                    print(f"\n[Tape detectada no slot {forced_slot}] volser={volser}\n")
                    return (forced_slot, volser)

            attempts += 1
            if max_attempts and attempts >= max_attempts:
                raise RuntimeError(
                    f"Máximo de tentativas ({max_attempts}) atingido aguardando "
                    f"tape no slot {forced_slot}."
                )
            self.log.info(
                "Aguardando tape no slot %d (tentativa %d) ...",
                forced_slot, attempts,
            )
            time.sleep(poll)
