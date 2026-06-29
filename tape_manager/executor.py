"""Orquestrador principal (MULTI-DRIVE PARALELO).

Fluxo de GRAVAÇÃO (multi-drive paralelo):
  1. Carrega plano (JSON persistido pelo Planner) — inclui
     drive_assignments e slot_assignments
  2. Carrega ProgressState (retoma de onde parou) — thread-safe
  3. Para cada drive em drive_assignments:
       - Cria thread independente
       - Thread processa seus volumes SEQUENCIALMENTE (dentro do drive):
         a. mtx load slot -> drive (serializado via mtx lock)
         b. sleep(5)
         c. TapeWriter.write_volume:
              - NÃO usa mt antes de tar -cvf (evita Device busy)
              - tar -cvf -T toc (retry 3x/10s se Device busy)
              - Pós-validação NÃO FATAL
         d. mtx unload slot <- drive (serializado via mtx lock)
         e. sleep(10)
         f. Atualizar ProgressState (thread-safe)
  4. Aguarda todas as threads terminarem

Fluxo de EXTRAÇÃO (multi-drive paralelo, via dd):
  1. Para cada drive informado:
       - Cria thread independente
       - Thread processa seus slots SEQUENCIALMENTE:
         a. mtx load slot -> drive (serializado)
         b. sleep(5)
         c. mt rewind (FUNCIONA para leitura)
         d. dd loop (bs=2M):
              - dd if=/dev/nstN of=<destdir>/<volser>_<filenum>.sgy bs=2M
              - Se 0 bytes -> EOF, remover, próximo slot
              - Se RC=0 -> .sgy
              - Se RC!=0 mas tem dados -> .err
         e. mtx unload (serializado)
         f. sleep(10)
  2. Aguarda todas as threads

Operações de menu (executor-level):
  - erase_tape(slot, nst_device, dte_index): mt erase + mtx unload
  - drive_status(nst_device, dte_index): mt status + oferece descarregar
  - list_available_drives(): lista /dev/nst* com DTE mtx correspondente

REGRAS CRÍTICAS MANTIDAS:
  - NUNCA mt antes de tar -cvf (Device busy no mhvtl)
  - mt rewind FUNCIONA antes de dd (leitura)
  - mtx unload sem mt offline
  - Retry 3x/10s para Device busy
  - _used_slots por drive (não reutilizar slot)
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from .config import Config
from .drive_resolver import DriveDevice, DriveResolver
from .exceptions import (
    NoTapeAvailableError,
    OperatorAbortError,
    TapeLoadError,
    TapeUnloadError,
)
from .integrity import IntegrityChecker, ProgressTracker
from .logger import get_logger
from .models import SurveyPlan, VolumePlan
from .mtx_controller import MTXController
from .tape_writer import TapeWriter


# ---------------------------------------------------------------------------
# Helpers de listagem de drives
# ---------------------------------------------------------------------------
def _natural_key(s) -> list:
    name = str(s)
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
class Executor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()
        self.mtx = MTXController(cfg)
        self.resolver = DriveResolver(cfg)
        self.integrity = IntegrityChecker(cfg)
        self._cancelled = False
        self._print_lock = threading.Lock()

    def cancel(self) -> None:
        self._cancelled = True

    # -----------------------------------------------------------------
    # API: listar drives disponíveis
    # -----------------------------------------------------------------
    @staticmethod
    def _is_base_nst_device(name: str) -> bool:
        """Retorna True se o device é um /dev/nstN base (sem sufixo de modo).

        O Linux st driver cria 4 variantes por drive físico:
          nstN  = modo 0 (padrão)
          nstNl = modo 1 (l)
          nstNm = modo 2 (m)
          nstNa = modo 3 (a)

        Todos apontam para o MESMO hardware. Filtramos as variantes
        para evitar confusão no menu e colisão de DTE.
        """
        # Remove prefixo "nst"
        suffix = name.removeprefix("nst")
        # Base device: apenas dígitos (ex: "0", "1", "12")
        return suffix.isdigit()

    def list_available_drives(self) -> list[dict]:
        """Lista todos os /dev/nstN base disponíveis com seus DTE mtx.

        FILTRA variantes de modo (nst0l, nst0m, nst0a) — estas são
        aliases do mesmo drive físico que nst0.

        Retorna lista de dicts:
          {"nst": "/dev/nst0", "st": "/dev/st0", "drive_number": 0,
           "dte_index": 0|None, "loaded": False, "volser": None}
        """
        self.mtx.discover_changer()
        try:
            status = self.mtx.status()
        except Exception as exc:
            self.log.warning("Falha ao obter mtx status: %s", exc)
            status = None

        drives: list[dict] = []
        nst_devices = sorted(Path("/dev").glob("nst*"),
                             key=lambda p: _natural_key(p.name))
        for nst_path in nst_devices:
            if not nst_path.is_char_device():
                continue
            # FILTRA variantes de modo: só lista nstN (digits only)
            if not self._is_base_nst_device(nst_path.name):
                self.log.debug("Filtrando variante de modo: %s", nst_path)
                continue
            num = nst_path.name.removeprefix("nst")
            st_path = f"/dev/st{num}"
            dte_index = None
            loaded = False
            volser = None
            loaded_slot = None
            if status is not None:
                dte_index = self.mtx.find_dte_for_drive(str(nst_path))
                if dte_index is not None:
                    for d in status.drives:
                        if d.drive == dte_index:
                            loaded = (d.status == "Full")
                            volser = d.volume_tag
                            loaded_slot = d.loaded_slot
                            break
            drives.append({
                "nst": str(nst_path),
                "st": st_path,
                "drive_number": int(num) if num.isdigit() else 0,
                "dte_index": dte_index,
                "loaded": loaded,
                "volser": volser,
                "loaded_slot": loaded_slot,
            })

        # (A correlação mhvtl agora é feita dentro de find_dte_for_drive
        # — não precisa mais de fallback separado aqui.)

        # FILTRO: esconde drives inacessíveis (DTE=None) do menu.
        # Estes são drives mhvtl que não têm DTE correspondente no changer
        # físico (ex: library 30 quando só há 1 changer controlando library 10).
        accessible_drives = [d for d in drives if d["dte_index"] is not None]
        hidden_count = len(drives) - len(accessible_drives)
        if hidden_count > 0:
            self.log.info(
                "Ocultando %d drive(s) inacessível(is) (DTE=None) do menu",
                hidden_count,
            )
            for d in drives:
                if d["dte_index"] is None:
                    self.log.debug("  Oculto: %s (DTE=None)", d["nst"])
        drives = accessible_drives

        # AVISO de colisão de DTE: se múltiplos /dev/nst* mapeiam para o
        # mesmo DTE, alerta o operador ANTES de atribuir drives no plano.
        dte_count: dict[int, list[str]] = {}
        for d in drives:
            if d["dte_index"] is not None:
                dte_count.setdefault(d["dte_index"], []).append(d["nst"])
        collisions = {dte: nsts for dte, nsts in dte_count.items()
                      if len(nsts) > 1}
        if collisions:
            self.log.warning(
                "COLISÃO DE DTE em list_available_drives: %s",
                collisions,
            )
            print("\n  >>> AVISO: Múltiplos /dev/nst* mapeiam para o MESMO DTE físico:")
            for dte, nsts in collisions.items():
                print(f"      DTE mtx {dte}: {nsts}")
            print("      Use APENAS UM deles no plano — paralelismo é impossível.")
            print()
        return drives


    def _make_drive_device(self, nst_device: str) -> DriveDevice:
        """Cria um DriveDevice a partir do caminho /dev/nstN."""
        num_str = nst_device.removeprefix("/dev/nst")
        drive_number = int(num_str) if num_str.isdigit() else 0
        return DriveDevice(
            serial="",
            nst_device=nst_device,
            st_device=f"/dev/st{num_str}",
            drive_number=drive_number,
        )

    # -----------------------------------------------------------------
    # API: executar plano (MULTI-DRIVE PARALELO)
    # -----------------------------------------------------------------
    def execute_plan(self, plan: SurveyPlan) -> None:
        """Executa (ou retoma) um plano completo de gravação multi-drive.

        Usa drive_assignments do plano para distribuir volumes entre drives.
        Cada drive roda em uma thread independente.
        """
        self.log.info(
            "==== Iniciando execução MULTI-DRIVE do plano: %s ====",
            plan.plan_file,
        )

        # Descobrir changer + inventory
        self.mtx.discover_changer()
        self.mtx.inventory()

        # Se não há drive_assignments, cria uma atribuição padrão
        # (todos os volumes no primeiro drive disponível)
        if not plan.drive_assignments:
            self.log.warning(
                "Plano sem drive_assignments. Criando atribuição padrão "
                "(todos os volumes no primeiro drive disponível)."
            )
            drives = self.list_available_drives()
            if not drives:
                raise TapeLoadError("Nenhum drive /dev/nst* disponível.")
            first_nst = drives[0]["nst"]
            plan.drive_assignments = {
                first_nst: [v.index for v in plan.volumes]
            }
            self.log.info("Atribuição padrão: %s -> %s",
                          first_nst, plan.drive_assignments[first_nst])

        self.log.info("Volumes total=%d pendentes=%d",
                      len(plan.volumes), len(plan.pending_volumes))
        self.log.info("Drive assignments: %s", plan.drive_assignments)
        if plan.slot_assignments:
            self.log.info("Slot assignments: %s", plan.slot_assignments)

        # Resolver DTE para cada drive
        drive_dte: dict[str, int] = {}
        invalid_drives: list[str] = []
        for nst_device in plan.drive_assignments:
            dte = self.mtx.find_dte_for_drive(nst_device)
            if dte is None:
                # Drive não tem DTE válido neste changer — marca como inválido
                # e será consolidado (volumes redistribuídos para drives válidos)
                self.log.warning(
                    "Drive %s NÃO tem DTE mtx válido neste changer. "
                    "Será consolidado em outro drive.",
                    nst_device,
                )
                invalid_drives.append(nst_device)
                continue
            drive_dte[nst_device] = dte
            self.log.info("Drive %s -> DTE mtx %d", nst_device, dte)

        # CONSOLIDAÇÃO: se algum drive é inválido, redistribui seus volumes
        # para um drive válido (primeiro por ordem alfabética).
        if invalid_drives:
            valid_drives = [d for d in plan.drive_assignments.keys()
                            if d not in invalid_drives]
            if not valid_drives:
                raise TapeLoadError(
                    f"NENHUM drive válido no plano. Drives inválidos: {invalid_drives}. "
                    f"Verifique o changer (/dev/sg*) e a correlação mhvtl."
                )
            rescue_drive = sorted(valid_drives)[0]
            # Coleta volumes dos drives inválidos
            orphan_vols: list[int] = []
            for inv in invalid_drives:
                for v in plan.drive_assignments.get(inv, []):
                    if v not in orphan_vols:
                        orphan_vols.append(v)
            # Adiciona ao drive de resgate (sem duplicar)
            existing = plan.drive_assignments.get(rescue_drive, [])
            for v in orphan_vols:
                if v not in existing:
                    existing.append(v)
            plan.drive_assignments[rescue_drive] = sorted(existing)
            # Remove drives inválidos do plano
            for inv in invalid_drives:
                del plan.drive_assignments[inv]

            msg = (f"Drives inválidos {invalid_drives} consolidados em "
                   f"{rescue_drive}. Volumes: {plan.drive_assignments[rescue_drive]}")
            self.log.warning(msg)
            print(f"\n>>> AVISO: {msg}")
            print()
            # Persiste plano corrigido
            try:
                json_path = Path(plan.output_dir) / "plan.json"
                plan.to_json(json_path)
                self.log.info("Plano consolidado persistido: %s", json_path)
                print(f">>> Plano consolidado salvo em: {json_path}")
                print(f">>> Drive assignments (novo): {plan.drive_assignments}")
            except Exception as exc:
                self.log.warning("Falha ao persistir plano consolidado: %s", exc)
            print()

        # VALIDAÇÃO CRÍTICA: detectar colisão de DTE (múltiplos /dev/nst*
        # mapeando para o mesmo DTE físico). Se detectado, recusar paralelismo
        # e consolidar em execução sequencial single-drive.
        dte_to_drives: dict[int, list[str]] = {}
        for nst, dte in drive_dte.items():
            dte_to_drives.setdefault(dte, []).append(nst)
        collisions = {dte: nsts for dte, nsts in dte_to_drives.items()
                      if len(nsts) > 1}
        if collisions:
            # Colisão detectada — NÃO executar em paralelo.
            msg_lines = [
                "COLISÃO DE DTE DETECTADA — execução paralela cancelada:",
            ]
            for dte, nsts in collisions.items():
                msg_lines.append(
                    f"  DTE mtx {dte} está mapeado para {len(nsts)} devices: {nsts}"
                )
            msg_lines.append("")
            msg_lines.append(
                "Isto acontece quando múltiplos /dev/nst* são aliases do "
                "mesmo drive físico (ex: /dev/nst0 e /dev/nst0l) ou quando "
                "o mhvtl só tem 1 drive mas o plano pede 2+."
            )
            msg_lines.append("")
            # Escolhe o primeiro drive (menor nome) e consolida todos os
            # volumes nele — execução sequencial.
            all_vols: list[int] = []
            for vols in plan.drive_assignments.values():
                for v in vols:
                    if v not in all_vols:
                        all_vols.append(v)
            all_vols.sort()
            chosen_drive = sorted(plan.drive_assignments.keys())[0]
            msg_lines.append(
                f"Consolidando em execução SEQUENCIAL no drive {chosen_drive} "
                f"com {len(all_vols)} volume(s): {all_vols}"
            )
            for line in msg_lines:
                self.log.warning(line)
            print("\n>>> AVISO: " + "\n>>> ".join(msg_lines[1:]))
            print()

            # Reescreve drive_assignments para single-drive sequencial
            plan.drive_assignments = {chosen_drive: all_vols}
            drive_dte = {chosen_drive: drive_dte[chosen_drive]}
            # Persiste o plano corrigido para futuras execuções
            try:
                from .models import SurveyPlan
                json_path = Path(plan.output_dir) / "plan.json"
                plan.to_json(json_path)
                self.log.info("Plano consolidado persistido: %s", json_path)
                print(f">>> Plano consolidado salvo em: {json_path}")
                print(f">>> Drive assignments (novo): {plan.drive_assignments}")
            except Exception as exc:
                self.log.warning("Falha ao persistir plano consolidado: %s", exc)
            print()

        # Tracker compartilhado (thread-safe)
        tracker = ProgressTracker.for_plan(plan, self.cfg)

        # Estruturas compartilhadas para detecção de drives quebrados
        broken_drives: set[str] = set()
        broken_lock = threading.Lock()
        # Volumes que falharam e precisam ser reprocessados em outro drive
        failed_volumes: list[int] = []
        failed_lock = threading.Lock()

        # Lançar threads
        threads: list[threading.Thread] = []
        errors: list[dict] = []
        errors_lock = threading.Lock()

        for nst_device, vol_indices in plan.drive_assignments.items():
            dte = drive_dte[nst_device]
            t = threading.Thread(
                target=self._execute_drive_volumes,
                args=(plan, nst_device, dte, vol_indices, tracker,
                      errors, errors_lock,
                      broken_drives, broken_lock,
                      failed_volumes, failed_lock),
                name=f"drive-{nst_device}",
            )
            threads.append(t)
            t.start()

        # Aguardar todas as threads
        for t in threads:
            t.join()

        # FASE 2: Re-processar volumes que falharam devido a drives quebrados
        # em um drive que funcionou (se houver).
        if failed_volumes:
            working_drives = [d for d in plan.drive_assignments.keys()
                              if d not in broken_drives]
            if working_drives:
                rescue_drive = sorted(working_drives)[0]
                rescue_dte = drive_dte.get(rescue_drive)
                if rescue_dte is not None:
                    # Deduplica e ordena volumes falhados
                    unique_failed = sorted(set(failed_volumes))
                    self.log.warning(
                        "==== FASE 2: Re-processando %d volume(s) falhado(s) "
                        "no drive %s (DTE %d) — drives quebrados: %s ====",
                        len(unique_failed), rescue_drive, rescue_dte,
                        sorted(broken_drives),
                    )
                    print(f"\n>>> FASE 2: Re-processando {len(unique_failed)} "
                          f"volume(s) falhado(s) no drive {rescue_drive}")
                    print(f"    Drives quebrados: {sorted(broken_drives)}")
                    print(f"    Volumes: {unique_failed}")
                    print()

                    # Remove os volumes já reprocessados da lista de erros
                    with errors_lock:
                        errors[:] = [e for e in errors
                                     if e.get("volser") not in
                                     [plan.volumes[i-1].volser
                                      for i in unique_failed]]

                    # Executa sequencialmente no drive de resgate
                    self._execute_drive_volumes(
                        plan, rescue_drive, rescue_dte, unique_failed,
                        tracker, errors, errors_lock,
                        broken_drives, broken_lock,
                        failed_volumes, failed_lock,
                    )
            else:
                self.log.error(
                    "Nenhum drive funcionando para re-processar %d volume(s) "
                    "falhado(s). Drives quebrados: %s",
                    len(failed_volumes), sorted(broken_drives),
                )
                print(f"\n>>> ERRO: Todos os drives estão quebrados. "
                      f"{len(failed_volumes)} volume(s) não processado(s).")

        # Relatório final
        self.log.info("==== Execução concluída: %s ====", plan.plan_file)
        if errors:
            self.log.error("Erros durante execução multi-drive: %d", len(errors))
            for e in errors:
                self.log.error("  %s (drive %s): %s",
                               e.get("volser"), e.get("drive"), e.get("error"))
            print(f"\n>>> ATENÇÃO: {len(errors)} erro(s) durante execução.")
            for e in errors:
                err_short = e.get('error', '')[:80]
                print(f"    {e.get('volser')} (drive {e.get('drive')}): {err_short}")
        else:
            print("\n>>> Todos os volumes processados com sucesso.")

    def _execute_drive_volumes(
        self,
        plan: SurveyPlan,
        nst_device: str,
        dte_index: int,
        vol_indices: list[int],
        tracker: ProgressTracker,
        errors: list[dict],
        errors_lock: threading.Lock,
        broken_drives: set[str],
        broken_lock: threading.Lock,
        failed_volumes: list[int],
        failed_lock: threading.Lock,
    ) -> None:
        """Processa volumes sequencialmente dentro de um drive (thread).

        Cada drive tem seu próprio _used_slots (thread-local) para evitar
        reutilização de slots.

        DETECÇÃO DE DRIVE QUEBRADO: se um volume falha com Hardware Error
        no mtx load, o drive é marcado como quebrado e os volumes restantes
        são re-enfileirados para processamento em outro drive (fase 2).
        """
        drive_device = self._make_drive_device(nst_device)
        writer = TapeWriter(self.cfg, drive_device, self.integrity)
        used_slots: set[int] = set()

        self.log.info("[thread %s] Iniciando processamento de %d volume(s)",
                      nst_device, len(vol_indices))

        for idx in vol_indices:
            # Se este drive foi marcado como quebrado por outra thread,
            # não tenta processar mais volumes — enfileira para fase 2.
            with broken_lock:
                if nst_device in broken_drives:
                    self.log.warning(
                        "[thread %s] Drive marcado como quebrado. "
                        "Enfileirando volume %d para fase 2.",
                        nst_device, idx,
                    )
                    with failed_lock:
                        failed_volumes.append(idx)
                    continue

            if idx < 1 or idx > len(plan.volumes):
                self.log.error("[thread %s] Índice de volume %d fora do range (1-%d)",
                               nst_device, idx, len(plan.volumes))
                with errors_lock:
                    errors.append({
                        "volser": f"INDEX_{idx}",
                        "drive": nst_device,
                        "error": f"Índice {idx} fora do range",
                    })
                continue

            vol = plan.volumes[idx - 1]

            if tracker.is_volume_done(vol):
                self.log.info("[thread %s] Volume %s já concluído. Pulando.",
                              nst_device, vol.volser)
                continue

            try:
                self._process_volume_in_drive(
                    plan, vol, writer, tracker,
                    drive_device, dte_index, used_slots,
                )
            except Exception as exc:
                exc_str = str(exc)
                self.log.error("[thread %s] Erro no volume %s: %s",
                               nst_device, vol.volser, exc_str)

                # Detecta erros fatais de mtx load → drive quebrado
                # Hardware Error: drive físico/driver mhvtl com problema
                # illegal drive-number: DTE não existe neste changer
                # No medium found: drive sem tape (mas mtx falhou em carregar)
                is_broken_error = (
                    ("Hardware Error" in exc_str and "MOVE MEDIUM" in exc_str)
                    or "illegal <drive-number> argument" in exc_str
                    or ("No medium found" in exc_str and "mtx load" in exc_str)
                )
                if is_broken_error:
                    self.log.error(
                        "[thread %s] Erro fatal no DTE %d. "
                        "Marcando drive como QUEBRADO. Volumes restantes "
                        "serão reprocessados na fase 2.",
                        nst_device, dte_index,
                    )
                    with broken_lock:
                        broken_drives.add(nst_device)
                    # Enfileira ESTE volume e todos os restantes
                    with failed_lock:
                        failed_volumes.append(idx)
                        # Adiciona volumes restantes deste drive
                        remaining = vol_indices[vol_indices.index(idx) + 1:]
                        failed_volumes.extend(remaining)
                    with errors_lock:
                        errors.append({
                            "volser": vol.volser,
                            "drive": nst_device,
                            "error": f"DRIVE QUEBRADO (Hardware Error): {exc_str[:100]}",
                        })
                    break  # Sai do loop — não tenta mais volumes neste drive
                else:
                    with errors_lock:
                        errors.append({
                            "volser": vol.volser,
                            "drive": nst_device,
                            "error": exc_str,
                        })

        self.log.info("[thread %s] Concluído.", nst_device)

    def _process_volume_in_drive(
        self,
        plan: SurveyPlan,
        vol: VolumePlan,
        writer: TapeWriter,
        tracker: ProgressTracker,
        drive: DriveDevice,
        dte_index: int,
        used_slots: set[int],
    ) -> None:
        """Processa um volume em um drive específico."""
        self.log.info("[drive %s] >>> Processando volume %s <<<",
                      drive.preferred, vol.volser)
        if self._cancelled:
            raise OperatorAbortError(
                f"Cancelado antes do volume {vol.volser}"
            )

        tracker.mark_volume_started(vol)

        # Carregar tape (usa slot_assignments se definido)
        slot, loaded_volser = self._ensure_tape_loaded_for_drive(
            plan, vol.volser, vol.index, dte_index, used_slots
        )
        try:
            writer.write_volume(plan, vol, tracker)
        finally:
            # Sempre descarrega ao final (sucesso ou falha)
            self._safe_unload_for_drive(slot, loaded_volser, dte_index)
            time.sleep(10)

        tracker.mark_volume_completed(vol)

    def _ensure_tape_loaded_for_drive(
        self,
        plan: SurveyPlan,
        volser: str,
        vol_index: int,
        dte_index: int,
        used_slots: set[int],
    ) -> tuple[int, str]:
        """Garante que uma tape está carregada no drive (DTE dte_index).

        MULTI-DRIVE:
          1. Usa slot_assignments (plan.slot_for_volume(vol_index)) se definido.
          2. Se há tape no drive, descarrega usando mtx unload (NÃO mt offline).
          3. mtx load slot -> drive
          4. sleep(5) após load

        Retorna (slot, volser) da tape carregada.
        """
        forced_slot = plan.slot_for_volume(vol_index)
        if forced_slot is not None:
            self.log.info(
                "[drive DTE %d] Slot forçado do slot_assignments: vol_index=%d -> slot=%d",
                dte_index, vol_index, forced_slot,
            )

        status = self.mtx.status()

        # Verifica se já há tape no drive
        existing_tape_in_drive = None
        for d in status.drives:
            if d.drive == dte_index and d.status == "Full":
                existing_tape_in_drive = d
                break

        if existing_tape_in_drive is not None:
            # Caso 1: mtx reporta slot de origem E bate com forced_slot -> reutiliza
            if (existing_tape_in_drive.loaded_slot is not None
                    and forced_slot is not None
                    and existing_tape_in_drive.loaded_slot == forced_slot):
                self.log.info(
                    "[drive DTE %d] Já contém tape do slot %d (volser=%s). Reutilizando.",
                    dte_index, existing_tape_in_drive.loaded_slot,
                    existing_tape_in_drive.volume_tag,
                )
                return (existing_tape_in_drive.loaded_slot,
                        existing_tape_in_drive.volume_tag or volser)

            # Caso 2: há tape no drive mas não é a que queremos. Descarregar.
            self.log.info(
                "[drive DTE %d] Contém tape mas não é a desejada. Descarregando.",
                dte_index,
            )
            self._unload_drive_to_free_slot_for_drive(status, dte_index)
            status = self.mtx.status()

        # Determinar slot de origem
        slot: int | None = None
        loaded_volser: str = volser

        if forced_slot is not None:
            # Slot forçado: verifica se tem tape nele
            slot_full = any(
                se.slot == forced_slot and se.full
                for se in status.storage_elements
            )
            if slot_full:
                slot = forced_slot
                for se in status.storage_elements:
                    if se.slot == forced_slot and se.full:
                        loaded_volser = se.volume_tag or volser
                        break
                self.log.info(
                    "[drive DTE %d] Slot forçado %d tem tape (volser=%s).",
                    dte_index, forced_slot, loaded_volser,
                )
            else:
                # Slot forçado vazio -> erro (multi-drive não pede ao operador)
                self.log.error(
                    "[drive DTE %d] Slot forçado %d está vazio. "
                    "Operador deve posicionar a tape antes da execução.",
                    dte_index, forced_slot,
                )
                raise NoTapeAvailableError(
                    expected_slot=forced_slot,
                    volser_hint=volser,
                )
        else:
            # Sem slot forçado: auto-pick (primeiro slot ocupado não usado por este drive)
            available = None
            for se in status.storage_elements:
                if se.full and se.slot not in used_slots:
                    available = (se.slot, se.volume_tag or f"SLOT{se.slot:03d}")
                    break
            if available is None:
                self.log.error(
                    "[drive DTE %d] Nenhuma tape disponível (não usada) para %s. "
                    "Slots já usados: %s.",
                    dte_index, volser, sorted(used_slots),
                )
                raise NoTapeAvailableError(volser_hint=volser)
            slot, loaded_volser = available

        used_slots.add(slot)
        self.log.info(
            "[drive DTE %d] auto_pick/forced: usando slot %d (volser=%s). "
            "Slots já usados: %s",
            dte_index, slot, loaded_volser, sorted(used_slots),
        )

        # mtx load slot -> drive
        self.log.info("[drive DTE %d] LOAD slot=%d (volser=%s)",
                      dte_index, slot, loaded_volser)
        try:
            self.mtx.load(slot, dte_index)
        except TapeLoadError as exc:
            self.log.error("[drive DTE %d] Falha no load: %s", dte_index, exc)
            raise

        # sleep(5) após load
        time.sleep(5)

        return slot, loaded_volser

    def _unload_drive_to_free_slot_for_drive(
        self, status, drive_num: int
    ) -> None:
        """Descarrega a tape do drive para um slot livre usando mtx unload.

        NÃO usa mt offline. Se não houver slot livre, tenta devolver
        para o slot de origem (se conhecido).
        """
        existing = None
        for d in status.drives:
            if d.drive == drive_num and d.status == "Full":
                existing = d
                break

        if existing is not None and existing.loaded_slot is not None:
            try:
                self.mtx.unload(existing.loaded_slot, drive_num)
                self.log.info(
                    "[drive DTE %d] Tape descarregada para slot de origem %d",
                    drive_num, existing.loaded_slot,
                )
                time.sleep(2)
                return
            except TapeUnloadError as exc:
                self.log.warning(
                    "[drive DTE %d] Unload para slot %d falhou: %s. "
                    "Tentando slot livre.", drive_num, existing.loaded_slot, exc,
                )

        free_slot = status.first_free_slot()
        if free_slot is None:
            self.log.error(
                "[drive DTE %d] Nenhum slot livre para descarregar.", drive_num,
            )
            raise TapeUnloadError(
                f"Nenhum slot livre para descarregar DTE {drive_num}"
            )

        try:
            self.mtx.unload(free_slot, drive_num)
            self.log.info(
                "[drive DTE %d] Tape descarregada para slot livre %d",
                drive_num, free_slot,
            )
            time.sleep(2)
        except TapeUnloadError as exc:
            self.log.error(
                "[drive DTE %d] Unload para slot livre %d falhou: %s. "
                "Sem mt offline. Operador precisa intervir.",
                drive_num, free_slot, exc,
            )
            raise

    def _safe_unload_for_drive(
        self, slot: int | None, volser: str, dte_index: int
    ) -> None:
        """Descarrega tape do DTE dte_index com tolerância a falhas.

        NÃO usa mt offline. Apenas mtx unload.
        """
        if not slot or slot <= 0:
            self.log.warning(
                "[drive DTE %d] Slot inválido (%s). Procurando slot livre...",
                dte_index, slot,
            )
            try:
                status = self.mtx.status()
                free_slot = status.first_free_slot()
                if free_slot:
                    slot = free_slot
                else:
                    self.log.error(
                        "[drive DTE %d] Nenhum slot livre para unload.", dte_index,
                    )
                    return
            except Exception as e:
                self.log.error("Falha ao obter mtx status para unload: %s", e)
                return

        try:
            self.mtx.unload(slot, dte_index)
            self.log.info("[drive DTE %d] Tape %s descarregada para slot %d",
                          dte_index, volser, slot)
        except TapeUnloadError as exc:
            self.log.warning(
                "[drive DTE %d] Unload falhou (%s). Tentando slot livre alternativo.",
                dte_index, exc,
            )
            try:
                status = self.mtx.status()
                free_slot = status.first_free_slot()
                if free_slot and free_slot != slot:
                    self.log.info("[drive DTE %d] Tentando unload para slot livre %d",
                                  dte_index, free_slot)
                    self.mtx.unload(free_slot, dte_index)
                    self.log.info(
                        "[drive DTE %d] Tape %s descarregada para slot livre %d",
                        dte_index, volser, free_slot,
                    )
                else:
                    self.log.error(
                        "[drive DTE %d] Unload persistente falhou para slot=%d: %s. "
                        "Operador precisa intervir.",
                        dte_index, slot, exc,
                    )
            except Exception as inner:
                self.log.error(
                    "[drive DTE %d] Unload persistente falhou: %s",
                    dte_index, inner,
                )

    # -----------------------------------------------------------------
    # API: extrair tapes (MULTI-DRIVE PARALELO via dd)
    # -----------------------------------------------------------------
    def extract_slots_multidrive(
        self,
        drive_slots_map: dict[str, list[int]],
        output_dir: str | Path,
    ) -> dict:
        """Extrai tapes em paralelo usando dd.

        Args:
          drive_slots_map: {"/dev/nst0": [1, 2, 3], "/dev/nst1": [4, 5, 6]}
          output_dir: diretório de destino

        Retorna dict com:
          - batch_id: identificador do batch
          - status: completed | partial | failed
          - total_files: total de arquivos extraídos
          - total_bytes: total de bytes extraídos
          - manifests: lista de manifestos (um por tape)
          - per_drive: {"/dev/nst0": [manifestos], ...}

        DETECÇÃO DE DRIVE QUEBRADO: se um drive falha consistentemente
        com Hardware Error no mtx load, os slots restantes são
        redistribuídos para um drive que funcionou (fase 2).
        """
        from .tape_extractor import TapeExtractor

        self.mtx.discover_changer()
        self.mtx.inventory()

        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        # Obter volser para cada slot ANTES de carregar
        status = self.mtx.status()
        slot_volser: dict[int, str] = {}
        for se in status.storage_elements:
            if se.full:
                slot_volser[se.slot] = se.volume_tag or f"SLOT{se.slot:03d}"

        # Resolver DTE para cada drive (com consolidação de drives inválidos)
        drive_dte: dict[str, int] = {}
        invalid_drives: list[str] = []
        for nst_device in drive_slots_map:
            dte = self.mtx.find_dte_for_drive(nst_device)
            if dte is None:
                self.log.warning(
                    "Drive %s NÃO tem DTE mtx válido. Será consolidado.",
                    nst_device,
                )
                invalid_drives.append(nst_device)
                continue
            drive_dte[nst_device] = dte

        # Consolidação: redistribui slots de drives inválidos
        if invalid_drives:
            valid_drives = [d for d in drive_slots_map.keys()
                            if d not in invalid_drives]
            if not valid_drives:
                raise TapeLoadError(
                    f"NENHUM drive válido. Inválidos: {invalid_drives}"
                )
            rescue_drive = sorted(valid_drives)[0]
            orphan_slots: list[int] = []
            for inv in invalid_drives:
                for s in drive_slots_map.get(inv, []):
                    if s not in orphan_slots:
                        orphan_slots.append(s)
            existing = drive_slots_map.get(rescue_drive, [])
            for s in orphan_slots:
                if s not in existing:
                    existing.append(s)
            drive_slots_map[rescue_drive] = sorted(existing)
            for inv in invalid_drives:
                del drive_slots_map[inv]
            msg = (f"Drives inválidos {invalid_drives} consolidados em "
                   f"{rescue_drive}. Slots: {drive_slots_map[rescue_drive]}")
            self.log.warning(msg)
            print(f"\n>>> AVISO: {msg}\n")

        batch_id = f"batch_{int(time.time())}"

        # Estruturas compartilhadas para detecção de drives quebrados
        broken_drives: set[str] = set()
        broken_lock = threading.Lock()
        failed_slots: list[tuple[str, int]] = []  # (volser, slot)
        failed_lock = threading.Lock()

        # Lançar threads
        per_drive: dict[str, list] = {}
        per_drive_lock = threading.Lock()
        threads: list[threading.Thread] = []

        print(f"\n  Extração multi-drive: {len(drive_slots_map)} drive(s), "
              f"{sum(len(s) for s in drive_slots_map.values())} slot(s) total")
        for nst_device, slots in drive_slots_map.items():
            print(f"    {nst_device} (DTE {drive_dte[nst_device]}): slots {slots}")
        print()

        for nst_device, slots in drive_slots_map.items():
            dte = drive_dte[nst_device]
            t = threading.Thread(
                target=self._extract_drive_slots,
                args=(nst_device, dte, slots, slot_volser, output_dir,
                      per_drive, per_drive_lock,
                      broken_drives, broken_lock,
                      failed_slots, failed_lock),
                name=f"extract-{nst_device}",
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # FASE 2: Re-extrair slots que falharam em drives quebrados
        if failed_slots:
            working_drives = [d for d in drive_slots_map.keys()
                              if d not in broken_drives]
            if working_drives:
                rescue_drive = sorted(working_drives)[0]
                rescue_dte = drive_dte.get(rescue_drive)
                if rescue_dte is not None:
                    self.log.warning(
                        "==== FASE 2: Re-extraindo %d slot(s) falhado(s) "
                        "no drive %s (DTE %d) — drives quebrados: %s ====",
                        len(failed_slots), rescue_drive, rescue_dte,
                        sorted(broken_drives),
                    )
                    print(f"\n>>> FASE 2: Re-extraindo {len(failed_slots)} "
                          f"slot(s) falhado(s) no drive {rescue_drive}")
                    print(f"    Drives quebrados: {sorted(broken_drives)}")
                    print(f"    Slots: {[s for _, s in failed_slots]}")
                    print()

                    rescue_slots = [s for _, s in failed_slots]
                    self._extract_drive_slots(
                        rescue_drive, rescue_dte, rescue_slots,
                        slot_volser, output_dir,
                        per_drive, per_drive_lock,
                        broken_drives, broken_lock,
                        failed_slots, failed_lock,
                    )
            else:
                self.log.error(
                    "Nenhum drive funcionando para re-extrair %d slot(s).",
                    len(failed_slots),
                )
                print(f"\n>>> ERRO: Todos os drives quebrados. "
                      f"{len(failed_slots)} slot(s) não extraído(s).")

        # Agregar resultados
        all_manifests = []
        for nst_device, manifests in per_drive.items():
            all_manifests.extend(manifests)

        total_files = sum(
            sum(1 for f in m.files if f.extracted) for m in all_manifests
        )
        total_bytes = sum(
            sum(f.size for f in m.files if f.extracted) for m in all_manifests
        )

        ok = sum(1 for m in all_manifests if m.status == "ok" and m.files)
        errors = sum(1 for m in all_manifests if m.status == "error")
        empty = sum(1 for m in all_manifests if m.status == "ok" and not m.files)
        if errors == 0:
            batch_status = "completed"
        elif ok == 0 and empty == 0:
            batch_status = "failed"
        else:
            batch_status = "partial"

        # Persistir batch
        from .models import ExtractionBatch
        batch = ExtractionBatch(
            batch_id=batch_id,
            output_dir=str(output_dir),
            manifests=all_manifests,
            status=batch_status,
            total_files=total_files,
            total_bytes=total_bytes,
            finished_at=time.time(),
        )
        batch_path = output_dir / f"{batch_id}.json"
        try:
            batch.to_json(batch_path)
        except Exception as exc:
            self.log.warning("Falha ao persistir batch %s: %s", batch_path, exc)

        return {
            "batch_id": batch_id,
            "status": batch_status,
            "total_files": total_files,
            "total_bytes": total_bytes,
            "manifests": [m.to_dict() for m in all_manifests],
            "per_drive": {
                k: [m.to_dict() for m in v] for k, v in per_drive.items()
            },
        }

    def _extract_drive_slots(
        self,
        nst_device: str,
        dte_index: int,
        slots: list[int],
        slot_volser: dict[int, str],
        output_dir: Path,
        per_drive: dict[str, list],
        per_drive_lock: threading.Lock,
        broken_drives: set[str],
        broken_lock: threading.Lock,
        failed_slots: list[tuple[str, int]],
        failed_lock: threading.Lock,
    ) -> None:
        """Extrai tapes de múltiplos slots em um drive (thread).

        DETECÇÃO DE DRIVE QUEBRADO: se extract_tape falha com Hardware Error
        no mtx load, o drive é marcado como quebrado e os slots restantes
        são re-enfileirados para fase 2.
        """
        from .tape_extractor import TapeExtractor

        extractor = TapeExtractor(self.cfg, self.mtx)
        drive_results: list = []

        for slot in slots:
            # Se este drive foi marcado como quebrado, enfileira para fase 2
            with broken_lock:
                if nst_device in broken_drives:
                    self.log.warning(
                        "[extract %s] Drive quebrado. Enfileirando slot %d para fase 2.",
                        nst_device, slot,
                    )
                    volser = slot_volser.get(slot, f"SLOT{slot:03d}")
                    with failed_lock:
                        failed_slots.append((volser, slot))
                    continue

            volser = slot_volser.get(slot, f"SLOT{slot:03d}")
            manifest = extractor.extract_tape(
                slot=slot,
                drive_index=dte_index,
                nst_device=nst_device,
                output_dir=output_dir,
                volser=volser,
            )
            drive_results.append(manifest)

            # Detecta drive quebrado (Hardware Error no mtx load)
            if manifest.status == "error" and manifest.message:
                msg = manifest.message
                if ("Hardware Error" in msg and "MOVE MEDIUM" in msg) \
                   or "illegal <drive-number> argument" in msg:
                    self.log.error(
                        "[extract %s] Drive QUEBRADO (DTE %d). "
                        "Enfileirando slots restantes para fase 2.",
                        nst_device, dte_index,
                    )
                    with broken_lock:
                        broken_drives.add(nst_device)
                    # Enfileira ESTE slot e todos os restantes
                    with failed_lock:
                        failed_slots.append((volser, slot))
                        remaining = slots[slots.index(slot) + 1:]
                        for s in remaining:
                            v = slot_volser.get(s, f"SLOT{s:03d}")
                            failed_slots.append((v, s))
                    break  # Sai do loop — não tenta mais slots neste drive

        with per_drive_lock:
            # Se for a thread de resgate (fase 2), adiciona aos resultados
            # existentes do drive; senão, define a lista.
            if nst_device in per_drive:
                per_drive[nst_device].extend(drive_results)
            else:
                per_drive[nst_device] = drive_results

    # -----------------------------------------------------------------
    # API: apagar cartucho (mt erase)
    # -----------------------------------------------------------------
    def erase_tape(self, slot: int, nst_device: str | None = None,
                   dte_index: int | None = None) -> bool:
        """Apaga (erase) a tape do slot informado.

        Se nst_device/dte_index não informados, usa o primeiro drive disponível.

        Fluxo:
          1. mtx load slot -> drive
          2. sleep(5)
          3. mt -f /dev/nstN rewind
          4. mt -f /dev/nstN erase
          5. mt -f /dev/nstN rewind (deixa no BOT)
          6. mtx unload slot <- drive (sem mt offline!)
          7. sleep(10)

        Retorna True se sucesso, False caso contrário.
        """
        self.log.info("=== APAGAR tape: slot=%d ===", slot)
        self.mtx.discover_changer()

        # Resolver drive se não informado
        if nst_device is None or dte_index is None:
            drives = self.list_available_drives()
            if not drives:
                self.log.error("Nenhum drive /dev/nst* disponível.")
                return False
            nst_device = drives[0]["nst"]
            dte_index = drives[0]["dte_index"]

        if dte_index is None:
            self.log.error("Não foi possível resolver DTE para %s", nst_device)
            return False

        print(f"  Carregando tape do slot {slot} para {nst_device} (DTE {dte_index})...")
        try:
            self.mtx.load(slot, dte_index)
        except TapeLoadError as exc:
            self.log.error("Falha no load do slot %d: %s", slot, exc)
            print(f"  ERRO: {exc}")
            return False

        try:
            time.sleep(5)
            # mt rewind
            print("  Rebobinando tape...")
            if not self._mt_op(nst_device, "rewind"):
                print("  ERRO no rewind")
                return False
            # mt erase
            print("  Apagando tape (isto pode demorar)...")
            if not self._mt_op(nst_device, "erase"):
                print("  ERRO no erase")
                return False
            # mt rewind (deixa no BOT)
            print("  Rebobinando após erase...")
            self._mt_op(nst_device, "rewind")
            print("  Tape apagada com sucesso.")
            return True
        except Exception as exc:
            self.log.error("Erro durante erase do slot %d: %s", slot, exc)
            print(f"  ERRO: {exc}")
            return False
        finally:
            # mtx unload (sem mt offline!)
            print(f"  Descarregando tape para slot {slot}...")
            self._safe_unload_for_drive(slot, f"SLOT{slot:03d}", dte_index)
            time.sleep(10)

    def _mt_op(self, nst_device: str, operation: str,
               timeout: int = 7200) -> bool:
        """Executa mt -f <dev> <operation>. Retorna True se sucesso."""
        try:
            proc = subprocess.run(
                [self.cfg.drives.mt_bin, "-f", nst_device, operation],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.log.error("Timeout no mt %s para %s", operation, nst_device)
            return False
        if proc.returncode != 0:
            self.log.error(
                "mt %s falhou (RC=%d) para %s: %s",
                operation, proc.returncode, nst_device,
                (proc.stderr or "").strip()[:200],
            )
            return False
        self.log.info("mt %s OK para %s", operation, nst_device)
        return True

    # -----------------------------------------------------------------
    # API: status do drive (mt status completo)
    # -----------------------------------------------------------------
    def drive_status(self, nst_device: str | None = None,
                     dte_index: int | None = None) -> dict:
        """Mostra status completo do drive e oferece descarregar.

        Se nst_device/dte_index não informados, usa o primeiro drive disponível.

        Retorna dict com:
          - changer: device changer
          - drive_device: /dev/nstN
          - dte_index: índice mtx do DTE
          - mtx_status: status do DTE no mtx (Full/Empty, volser, slot)
          - mt_status: saída do mt -f /dev/nstN status
          - loaded: bool (se há tape no drive)
        """
        self.log.info("=== STATUS DO DRIVE ===")
        result = {
            "changer": None,
            "drive_device": None,
            "dte_index": None,
            "mtx_status": None,
            "mt_status": None,
            "loaded": False,
        }

        try:
            result["changer"] = self.mtx.discover_changer()
        except Exception as exc:
            self.log.error("Falha ao descobrir changer: %s", exc)

        # Resolver drive se não informado
        if nst_device is None or dte_index is None:
            drives = self.list_available_drives()
            if drives:
                nst_device = nst_device or drives[0]["nst"]
                dte_index = dte_index or drives[0]["dte_index"]

        result["drive_device"] = nst_device
        result["dte_index"] = dte_index

        if nst_device is None:
            self.log.error("Nenhum drive disponível.")
            return result

        # mtx status (do DTE específico)
        try:
            status = self.mtx.status()
            if dte_index is not None:
                for d in status.drives:
                    if d.drive == dte_index:
                        result["mtx_status"] = {
                            "drive": d.drive,
                            "status": d.status,
                            "volume_tag": d.volume_tag,
                            "loaded_slot": d.loaded_slot,
                        }
                        result["loaded"] = (d.status == "Full")
                        break
        except Exception as exc:
            self.log.error("Falha no mtx status: %s", exc)

        # mt status (se há tape carregada)
        if result["loaded"]:
            try:
                proc = subprocess.run(
                    [self.cfg.drives.mt_bin, "-f", nst_device, "status"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=30,
                    check=False,
                )
                result["mt_status"] = (proc.stdout or "") + (proc.stderr or "")
            except subprocess.TimeoutExpired:
                result["mt_status"] = "(timeout no mt status)"
            except Exception as exc:
                result["mt_status"] = f"(erro: {exc})"

        return result

    def unload_drive(self, nst_device: str | None = None,
                     dte_index: int | None = None) -> bool:
        """Descarrega a tape do drive usando mtx unload (sem mt offline).

        Se nst_device/dte_index não informados, usa o primeiro drive disponível.
        """
        if nst_device is None or dte_index is None:
            drives = self.list_available_drives()
            if not drives:
                self.log.error("Nenhum drive disponível.")
                return False
            nst_device = nst_device or drives[0]["nst"]
            dte_index = dte_index or drives[0]["dte_index"]

        if dte_index is None:
            self.log.error("DTE não resolvido.")
            return False

        try:
            status = self.mtx.status()
        except Exception as exc:
            self.log.error("Falha no mtx status: %s", exc)
            return False

        # Encontra o DTE com tape
        dte_full = None
        for d in status.drives:
            if d.drive == dte_index and d.status == "Full":
                dte_full = d
                break

        if dte_full is None:
            self.log.info("Drive DTE %d já está vazio.", dte_index)
            return True

        # Tenta slot de origem
        if dte_full.loaded_slot is not None:
            try:
                self.mtx.unload(dte_full.loaded_slot, dte_index)
                self.log.info(
                    "Tape descarregada do DTE %d para slot de origem %d",
                    dte_index, dte_full.loaded_slot,
                )
                return True
            except TapeUnloadError as exc:
                self.log.warning(
                    "Unload para slot %d falhou: %s. Tentando slot livre.",
                    dte_full.loaded_slot, exc,
                )

        # Slot livre
        free_slot = status.first_free_slot()
        if free_slot is None:
            self.log.error("Nenhum slot livre para descarregar.")
            return False
        try:
            self.mtx.unload(free_slot, dte_index)
            self.log.info(
                "Tape descarregada do DTE %d para slot livre %d",
                dte_index, free_slot,
            )
            return True
        except TapeUnloadError as exc:
            self.log.error("Unload para slot livre %d falhou: %s",
                           free_slot, exc)
            return False
