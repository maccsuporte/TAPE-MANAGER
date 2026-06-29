"""Entry point: menu interativo.

Uso:
    python -m tape_manager                         # menu interativo
    python -m tape_manager --config caminho.yaml   # config custom
    python -m tape_manager --version
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Config
from .logger import get_logger, setup_logging


# ---------------------------------------------------------------------------
# Parser de argumentos
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    # Classe customizada para mostrar instruções de uso em caso de erro
    class TapeManagerArgumentParser(argparse.ArgumentParser):
        def error(self, message: str) -> None:
            print(f"\n{'='*60}")
            print(f"  ERRO: {message}")
            print(f"{'='*60}")
            _print_usage_instructions()
            sys.exit(2)

    p = TapeManagerArgumentParser(
        prog="tape_manager",
        description="Tape Manager - gestão modular de tape library (multi-drive)",
        add_help=True,
    )
    p.add_argument("--config", "-c", default=str(DEFAULT_CONFIG_PATH),
                   help="Caminho para o arquivo de configuração YAML")
    p.add_argument("--version", action="version", version="tape_manager 2.0.0")
    p.add_argument("--mhvtl", "-m", action="store_true", default=False,
                   help="Habilita modo mhvtl (Virtual Tape Library). "
                        "Sem este flag, opera apenas com drives físicos reais "
                        "(correlação via SCSI host) e esconde cartuchos "
                        "virtuais (JRT01, dummy). Atalho: -m")
    p.add_argument("--command", "-C",
                   help="Executa um comando direto sem menu "
                        "(status|inventory|list_drives|create_plan|"
                        "run_plan|extract|erase|drive_status)")
    p.add_argument("--survey-dir", help="Diretório do survey (create_plan)")
    p.add_argument("--cartridge", help="Tipo de cartucho (create_plan)")
    p.add_argument("--format", choices=["tar", "ltfs"], help="Formato (create_plan)")
    p.add_argument("--plan-file", help="Arquivo de plano (run_plan)")

    p.add_argument("--slots", help="Lista de slots separados por vírgula (extract/erase)")
    p.add_argument("--output-dir", help="Diretório de saída (extract)")
    return p


def _print_usage_instructions() -> None:
    """Imprime instruções de uso detalhadas com descrições de cada opção."""
    print(f"""
{'='*60}
  Tape Manager - Instruções de Uso
{'='*60}

  MODO DE OPERAÇÃO:
    python -m tape_manager            (modo físico - drives reais)
    python -m tape_manager --mhvtl    (modo mhvtl - Virtual Tape Library)
    python -m tape_manager -m         (atalho para --mhvtl)

  COMANDOS DIRETOS (--command):
    --command status        (exibe status da tape library)
    --command inventory     (executa inventário da biblioteca)
    --command list_drives   (lista drives /dev/nst* disponíveis)
    --command drive_status  (status detalhado de um drive específico)

  OPERAÇÕES DE GRAVAÇÃO:
    --command create_plan   (cria plano de gravação)
        Requer: --survey-dir, --cartridge, --format
        Opcional: --output-dir
    --command run_plan      (executa plano de gravação)
        Requer: --plan-file

  OPERAÇÕES DE LEITURA/LIMPEZA:
    --command extract       (extrai tapes para disco via dd)
        Requer: --slots, --output-dir
    --command erase         (apaga cartucho via mt erase)
        Requer: --slots

  MENU INTERATIVO:
    python -m tape_manager   (sem --command: abre menu com 11 opções)

  CARTUCHOS DISPONÍVEIS:
    Modo físico:  JAE05, JAE06, JBE05, JBE06, JBE07, JCE07
    Modo mhvtl:   JAE05, JAE06, JBE05, JBE06, JBE07, JCE07, JRT01, dummy

  CONFIGURAÇÃO:
    Editar config/config.yaml (changer device, drives, capacidades)

  EXEMPLOS:
    # Status da library (modo físico)
    python -m tape_manager --command status

    # Listar drives (modo mhvtl)
    python -m tape_manager --mhvtl --command list_drives

    # Criar plano de gravação (modo mhvtl)
    python -m tape_manager --mhvtl \\
        --command create_plan \\
        --survey-dir /survay \\
        --cartridge auto \\
        --format tar

    # Executar plano
    python -m tape_manager --mhvtl \\
        --command run_plan \\
        --plan-file /root/root/tape_manager/output/survay/plan.json

    # Extrair tapes para disco
    python -m tape_manager --mhvtl \\
        --command extract \\
        --slots 1,2,3 \\
        --output-dir /restore

    # Menu interativo (modo mhvtl)
    python -m tape_manager --mhvtl

{'='*60}
""")


# ---------------------------------------------------------------------------
# Helpers de UI
# ---------------------------------------------------------------------------
def _print_header(title: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _pause() -> None:
    try:
        input("\n[ENTER] para voltar ao menu... ")
    except EOFError:
        pass


def _which(cmd: str) -> bool:
    """Verifica se o comando existe no PATH."""
    from shutil import which
    return which(cmd) is not None


def _natural_key(s) -> list:
    """Chave de ordenação natural (sg2 < sg10 < sg20)."""
    import re
    name = str(s)
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _parse_int_list(text: str) -> list[int]:
    """Faz parse de uma lista de inteiros separados por vírgula."""
    result = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            pass
    return result


# ---------------------------------------------------------------------------
# Comandos do menu
# ---------------------------------------------------------------------------
def cmd_status(cfg: Config) -> None:
    from .mtx_controller import MTXController
    mtx = MTXController(cfg)
    try:
        status = mtx.status()
    except Exception as exc:
        print(f"ERRO ao obter status: {exc}")
        return
    _print_header("Status da Tape Library")
    print(f"Changer: {mtx.changer}")
    print("\nStorage Elements:")
    for se in status.storage_elements:
        tag = f" tag={se.volume_tag}" if se.volume_tag else ""
        print(f"  Slot {se.slot:3d}: {'FULL' if se.full else 'EMPTY'}{tag}")
    print("\nDrives:")
    for d in status.drives:
        tag = f" tag={d.volume_tag}" if d.volume_tag else ""
        slot = f" from_slot={d.loaded_slot}" if d.loaded_slot else ""
        print(f"  Drive {d.drive}: {d.status}{tag}{slot}")
    _pause()


def cmd_inventory(cfg: Config) -> None:
    from .mtx_controller import MTXController
    mtx = MTXController(cfg)
    _print_header("Inventário da Tape Library")
    try:
        mtx.discover_changer()
        mtx.inventory()
        print("Inventário executado com sucesso.")
    except Exception as exc:
        print(f"ERRO: {exc}")
    _pause()


def cmd_list_drives(cfg: Config) -> None:
    """Lista todos os /dev/nst* disponíveis e seus DTE mtx correspondentes."""
    from .executor import Executor
    _print_header("Drives Disponíveis (/dev/nst*)")

    try:
        executor = Executor(cfg)
        drives = executor.list_available_drives()
    except Exception as exc:
        print(f"ERRO ao listar drives: {exc}")
        _pause()
        return

    if not drives:
        print("  (nenhum /dev/nst* encontrado)")
        _pause()
        return

    print(f"\n  {'#':<4} {'Device':<14} {'st':<14} {'DTE':<5} {'Status':<8} {'Volser':<16} {'Slot'}")
    print(f"  {'-'*4} {'-'*14} {'-'*14} {'-'*5} {'-'*8} {'-'*16} {'-'*6}")
    for i, d in enumerate(drives, 1):
        loaded = "Full" if d["loaded"] else "Empty"
        volser = d["volser"] or "-"
        slot = str(d["loaded_slot"]) if d["loaded_slot"] else "-"
        dte = str(d["dte_index"]) if d["dte_index"] is not None else "?"
        print(f"  {i:<4} {d['nst']:<14} {d['st']:<14} {dte:<5} {loaded:<8} {volser:<16} {slot}")

    # Mostrar também dispositivos SCSI genéricos
    print("\n>>> Dispositivos SCSI genéricos (/dev/sg*)")
    sg_devices = sorted(Path("/dev").glob("sg*"),
                        key=lambda p: _natural_key(p.name))
    if not sg_devices:
        print("  (nenhum /dev/sg* encontrado)")
    else:
        for sg in sg_devices:
            if sg.is_char_device():
                print(f"  {sg}")

    # mhvtl
    if cfg.mhvtl.enabled:
        try:
            from . import mhvtl as mhvtl_mod
            mcfg = mhvtl_mod.load_mhvtl(cfg.mhvtl.config_dir)
            if mcfg is not None:
                print(f"\n>>> mhvtl detectado (config_dir={mcfg.config_path})")
                mhvtl_loaded = mhvtl_mod.is_mhvtl_loaded()
                print(f"  Módulo kernel: {'carregado' if mhvtl_loaded else 'NÃO carregado'}")
                print(f"  Libraries: {len(mcfg.libraries)} | Drives: {len(mcfg.drives)}")
                if mcfg.drives:
                    if cfg.mhvtl.auto_correlate:
                        mcfg = mhvtl_mod.correlate_drives(mcfg)
                    print("\n  Drives mhvtl:")
                    for drv in mcfg.drives:
                        nst = drv.nst_device or "-"
                        sg = drv.sg_device or "-"
                        print(f"    Drive {drv.index}: {drv.vendor} {drv.product} "
                              f"serial={drv.serial} -> nst={nst} sg={sg}")
        except Exception as exc:
            print(f"\n  (mhvtl: {exc})")

    print()
    _pause()


def cmd_create_plan(cfg: Config) -> None:
    """Criar plano de gravação interativo (com multi-drive).

    Fluxo:
      1. Diretório do survey
      2. Seleção de cartucho (lista + auto para multi-cartucho)
      3. Formato (tar/ltfs)
      4. Plano é criado e mostrado (volumes, arquivos, tamanhos)
      5. Listar /dev/nst* disponíveis
      6. Perguntar quantos drives usar
      7. Para cada drive: informar quais volumes (por índice) e quais slots
      8. Salvar drive_assignments e slot_assignments no plan.json
    """
    from .planner import Planner
    from .executor import Executor
    import json

    _print_header("Criar Plano de Gravação (Multi-Drive)")

    # 1. Diretório do survey
    survey_dir = input("Diretório do survey: ").strip()
    if not survey_dir:
        print("Diretório inválido."); _pause(); return
    survey_path = Path(survey_dir).expanduser().resolve()
    print(f"  Survey (absoluto): {survey_path}")
    print(f"  Output dir:        {cfg.default_output_dir}")
    if not survey_path.is_dir():
        print(f"ERRO: '{survey_path}' não é um diretório ou não existe.")
        _pause(); return

    # 2. Seleção de cartucho
    print("\nCartuchos suportados:")
    print(cfg.format_cartridge_list())
    print("  (ou digite 'auto' para seleção automática multi-cartucho)")
    cartridge = input("\nTipo de cartucho (ex: JBE07 ou auto): ").strip()
    if not cartridge:
        cartridge = "auto"
    cartridge = cartridge.lower() if cartridge.lower() == "auto" else cartridge.upper()

    # 3. Formato
    fmt = input("Formato [tar/ltfs]: ").strip().lower() or "tar"
    if fmt not in ("tar", "ltfs"):
        print("Formato inválido."); _pause(); return

    # 4. Cria plano (volsers auto-gerados como vol001, vol002, ...)
    try:
        planner = Planner(cfg)
        plan = planner.create_plan(str(survey_path), cartridge, fmt)
    except Exception as exc:
        print(f"ERRO ao criar plano: {exc}")
        _pause(); return

    # Mostra resumo do plano
    print(f"\n{'='*60}")
    print(f"  Plano criado: {len(plan.volumes)} volumes, {plan.total_files} arquivos, "
          f"{plan.total_bytes / (1024**3):.2f} GiB")
    print(f"{'='*60}")
    for vol in plan.volumes:
        cart = vol.effective_cartridge or plan.cartridge
        print(f"  Vol {vol.index}: volser={vol.volser} cartucho={cart} "
              f"({vol.used_bytes/(1024**3):.2f} GiB, {len(vol.files)} arqs)")
    if plan.skipped_files:
        print(f"\n  !!! {len(plan.skipped_files)} arquivo(s) saltado(s):")
        for s in plan.skipped_files[:5]:
            print(f"      - {s['path']} ({s['size']/(1024**3):.2f} GiB)")
    print()

    # 5. Listar /dev/nst* disponíveis
    print()
    print(">>> Drives disponíveis (/dev/nst*):")
    try:
        executor = Executor(cfg)
        drives = executor.list_available_drives()
    except Exception as exc:
        print(f"  ERRO ao listar drives: {exc}")
        print("  Continuando sem drive_assignments (pode ser definido depois).")
        drives = []

    if not drives:
        print("  (nenhum /dev/nst* encontrado)")
        print("  Continuando sem drive_assignments.")
    else:
        for i, d in enumerate(drives, 1):
            loaded = " [Full]" if d["loaded"] else ""
            volser = f" volser={d['volser']}" if d["volser"] else ""
            print(f"  {i}. {d['nst']} (DTE={d['dte_index'] if d['dte_index'] is not None else '?'}){loaded}{volser}")

        # 6. Perguntar quantos drives usar
        print()
        try:
            num_drives_input = input(
                f"Quantos drives usar? [1-{len(drives)}, default=1]: "
            ).strip()
            num_drives = int(num_drives_input) if num_drives_input else 1
        except ValueError:
            num_drives = 1
        num_drives = max(1, min(num_drives, len(drives)))

        # 7. Para cada drive: informar volumes e slots
        drive_assignments: dict[str, list[int]] = {}
        slot_assignments: dict[str, int] = {}
        all_assigned_vols: set[int] = set()
        all_assigned_slots: set[int] = set()

        print()
        print(f"  Plano tem {len(plan.volumes)} volume(s):")
        for vol in plan.volumes:
            print(f"    Vol {vol.index}: {vol.volser} "
                  f"({vol.used_bytes/(1024**2):.1f} MB)")
        print()

        for di in range(num_drives):
            print(f"--- Drive {di + 1}/{num_drives} ---")
            # Selecionar dispositivo
            try:
                dev_idx_input = input(
                    f"  Escolha o dispositivo (1-{len(drives)}): "
                ).strip()
                dev_idx = int(dev_idx_input) if dev_idx_input else 1
            except ValueError:
                dev_idx = 1
            dev_idx = max(1, min(dev_idx, len(drives)))
            nst_device = drives[dev_idx - 1]["nst"]

            # Informar volumes
            vols_input = input(
                f"  {nst_device} - volumes (índices separados por vírgula): "
            ).strip()
            vol_indices = _parse_int_list(vols_input)
            if not vol_indices:
                print("    Nenhum volume informado. Pulando este drive.")
                continue

            # Validar índices
            valid_vols = []
            for vi in vol_indices:
                if vi < 1 or vi > len(plan.volumes):
                    print(f"    AVISO: índice {vi} fora do range (1-{len(plan.volumes)}). Ignorado.")
                    continue
                if vi in all_assigned_vols:
                    print(f"    AVISO: índice {vi} já atribuído a outro drive. Ignorado.")
                    continue
                valid_vols.append(vi)
                all_assigned_vols.add(vi)

            if not valid_vols:
                print("    Nenhum volume válido. Pulando este drive.")
                continue

            # Informar slots
            slots_input = input(
                f"  {nst_device} - slots para os volumes {valid_vols} "
                f"(separados por vírgula, mesma ordem): "
            ).strip()
            slots = _parse_int_list(slots_input)

            if len(slots) != len(valid_vols):
                print(f"    AVISO: {len(slots)} slots para {len(valid_vols)} volumes.")
                print(f"    Usando mapeamento automático (slot = índice do volume).")
                slots = valid_vols

            for vi, sl in zip(valid_vols, slots):
                if sl in all_assigned_slots:
                    print(f"    AVISO: slot {sl} já usado. Ignorando mapeamento.")
                    continue
                slot_assignments[str(sl)] = vi
                all_assigned_slots.add(sl)
                print(f"    {nst_device}: Vol {vi} ({plan.volumes[vi-1].volser}) -> slot {sl}")

            drive_assignments[nst_device] = valid_vols
            print()

        # 8. Salvar drive_assignments e slot_assignments no plan.json
        if drive_assignments:
            plan.drive_assignments = drive_assignments
            plan.slot_assignments = slot_assignments
            json_path = Path(plan.output_dir) / "plan.json"
            plan.to_json(json_path)
            print(f">>> Drive assignments salvos em: {json_path}")
            print(f"    drive_assignments: {drive_assignments}")
            print(f"    slot_assignments: {slot_assignments}")

            # Verifica se todos os volumes foram atribuídos
            unassigned = [v.index for v in plan.volumes
                          if v.index not in all_assigned_vols]
            if unassigned:
                print(f"\n    AVISO: {len(unassigned)} volume(s) sem drive: {unassigned}")
                print(f"    Eles não serão processados na execução.")

    # Validação
    try:
        planner.validate_plan(plan)
        print(f"\n>>> Validação: OK")
        print(f"  plan.json: {Path(plan.output_dir) / 'plan.json'}")
    except Exception as exc:
        print(f"\n>>> Validação: FALHOU - {exc}")
    _pause()


def _resolve_plan_json(plan_file: str) -> str | None:
    """Aceita plan.json, .plan.json (legado) ou plan.<cart>.<fmt>.txt."""
    p = Path(plan_file).expanduser().resolve()
    if not p.is_file():
        return None
    if p.suffix == ".json":
        return str(p)
    for candidate_name in ("plan.json", ".plan.json"):
        candidate = p.parent / candidate_name
        if candidate.is_file():
            return str(candidate)
    return None


def cmd_validate_plan(cfg: Config) -> None:
    from .planner import Planner
    from .models import SurveyPlan
    _print_header("Validar Plano")
    plan_file = input("Arquivo do plano (plan.json ou plan.*.txt): ").strip()
    if not plan_file:
        print("Arquivo inválido."); _pause(); return
    p = Path(plan_file).expanduser()
    if not p.is_file():
        print(f"Arquivo não encontrado: {p}"); _pause(); return

    json_path = _resolve_plan_json(str(p))
    if not json_path:
        print(f"ERRO: Não foi possível localizar o arquivo plan.json.")
        print(f"  Arquivo informado: {p}")
        _pause(); return

    try:
        plan = SurveyPlan.from_json(json_path)
        Planner(cfg).validate_plan(plan)
        print()
        print(f"Plano: {json_path}")
        print(f"  Survey:    {plan.survey_dir}")
        print(f"  Cartucho:  {plan.cartridge} / formato: {plan.fmt}")
        print(f"  Volumes:   {len(plan.volumes)}")
        print(f"  Arquivos:  {plan.total_files}")
        print(f"  Tamanho:   {plan.total_bytes / (1024**3):.2f} GiB")
        if plan.skipped_files:
            print(f"  Saltados:  {len(plan.skipped_files)} (maiores que a tape)")
        if plan.drive_assignments:
            print(f"  Drive assignments:")
            for nst, vols in plan.drive_assignments.items():
                print(f"    {nst}: volumes {vols}")
        if plan.slot_assignments:
            print(f"  Slot assignments: {plan.slot_assignments}")
        # Detalhes por volume
        for vol in plan.volumes:
            cart_info = f" [cart={vol.effective_cartridge}]" if \
                vol.effective_cartridge and vol.effective_cartridge != plan.cartridge else ""
            print(f"    Vol {vol.index}: {vol.volser} - "
                  f"{len(vol.files)} arq, "
                  f"{vol.used_bytes / (1024**2):.1f} MB{cart_info}")
        print()
        print("Plano VÁLIDO.")
    except Exception as exc:
        print(f"Plano INVÁLIDO: {exc}")
    _pause()


def cmd_run_plan(cfg: Config) -> None:
    """Executar plano (multi-drive paralelo)."""
    from .executor import Executor
    from .models import SurveyPlan
    _print_header("Executar Plano (Multi-Drive Paralelo)")
    plan_file = input("Arquivo do plano (plan.json ou plan.*.txt): ").strip()
    if not plan_file:
        print("Arquivo inválido."); _pause(); return
    p = Path(plan_file).expanduser()
    if not p.is_file():
        print(f"Arquivo não encontrado: {p}"); _pause(); return

    json_path = _resolve_plan_json(str(p))
    if not json_path:
        print(f"ERRO: Não foi possível localizar o arquivo plan.json junto a {p}")
        _pause(); return

    try:
        plan = SurveyPlan.from_json(json_path)
    except Exception as exc:
        print(f"ERRO ao carregar plano: {exc}")
        _pause(); return

    # Mostra resumo do plano
    print(f"\n  Plano: {json_path}")
    print(f"  Volumes: {len(plan.volumes)}")
    print(f"  Pendentes: {len(plan.pending_volumes)}")
    if plan.drive_assignments:
        print(f"  Drive assignments:")
        for nst, vols in plan.drive_assignments.items():
            print(f"    {nst}: volumes {vols}")
    else:
        print(f"  Drive assignments: (nenhum — será usado o primeiro drive disponível)")
    if plan.slot_assignments:
        print(f"  Slot assignments: {plan.slot_assignments}")

    print()
    print("  Volumes a processar:")
    for vol in plan.volumes:
        cart = vol.effective_cartridge or plan.cartridge
        status_icon = "OK" if vol.status in ("written", "verified") else \
                      "!!" if vol.status == "failed" else "--"
        print(f"    [{status_icon}] Vol {vol.index}: {vol.volser} "
              f"({cart}, {vol.used_bytes/(1024**2):.1f} MB, {len(vol.files)} arqs)")

    # Confirmação
    try:
        ans = input("\nConfirma execução multi-drive? (s/N): ").strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("s", "sim", "y", "yes"):
        print("Execução cancelada.")
        _pause(); return

    try:
        executor = Executor(cfg)
        executor.execute_plan(plan)
        print("\n>>> Execução concluída.")
    except Exception as exc:
        print(f"ERRO durante execução: {exc}")
    _pause()


def cmd_progress(cfg: Config) -> None:
    from .models import ProgressState
    _print_header("Verificar Progresso")
    plan_file = input("Arquivo do plano (plan.json ou plan.*.txt): ").strip()
    if not plan_file:
        print("Arquivo inválido."); _pause(); return
    p = Path(plan_file).expanduser()
    if not p.is_file():
        print(f"Arquivo não encontrado: {p}"); _pause(); return
    json_path = _resolve_plan_json(str(p))
    if not json_path:
        print(f"ERRO: plan.json não encontrado junto a {p}"); _pause(); return
    plan_dir = Path(json_path).parent
    progress_file = plan_dir / cfg.integrity.progress_filename
    if not progress_file.is_file():
        legacy = plan_dir / ".progress.json"
        if legacy.is_file():
            progress_file = legacy
    if not progress_file.is_file():
        print(f"Arquivo de progresso não encontrado: {progress_file}")
        print("  (execução ainda não foi iniciada para este plano)")
        _pause(); return
    try:
        st = ProgressState.load(progress_file)
        print(f"Volume atual:   {st.current_volume or '(nenhum)'}")
        print(f"Concluídos:     {len(st.completed_volumes)} -> {st.completed_volumes}")
        print(f"Falharam:       {len(st.failed_volumes)} -> {st.failed_volumes}")
        print(f"Trocas de tape: {st.tape_changes}")
        if st.written_files:
            print("Arquivos gravados por volume:")
            for vol, files in st.written_files.items():
                print(f"  {vol}: {len(files)} arquivo(s)")
        if st.notes:
            print("Notas:")
            for n in st.notes:
                print(f"  - {n}")
    except Exception as exc:
        print(f"ERRO: {exc}")
    _pause()


def cmd_resume(cfg: Config) -> None:
    from .executor import Executor
    from .models import SurveyPlan
    _print_header("Retomar Execução (Multi-Drive)")
    plan_file = input("Arquivo do plano (plan.json ou plan.*.txt): ").strip()
    if not plan_file:
        print("Arquivo inválido."); _pause(); return
    p = Path(plan_file).expanduser()
    if not p.is_file():
        print(f"Arquivo não encontrado: {p}"); _pause(); return
    json_path = _resolve_plan_json(str(p))
    if not json_path:
        print(f"ERRO: plan.json não encontrado junto a {p}"); _pause(); return
    try:
        plan = SurveyPlan.from_json(json_path)
        executor = Executor(cfg)
        executor.execute_plan(plan)
        print("Retomada concluída.")
    except Exception as exc:
        print(f"ERRO: {exc}")
    _pause()


def cmd_extract(cfg: Config) -> None:
    """Extrair tapes para disco (multi-drive paralelo, via dd)."""
    from .executor import Executor
    _print_header("Extrair Tapes para Disco (Multi-Drive, dd)")

    # 1. Listar /dev/nst* disponíveis
    print(">>> Drives disponíveis (/dev/nst*):")
    try:
        executor = Executor(cfg)
        drives = executor.list_available_drives()
    except Exception as exc:
        print(f"ERRO ao listar drives: {exc}")
        _pause(); return

    if not drives:
        print("  (nenhum /dev/nst* encontrado)")
        _pause(); return

    for i, d in enumerate(drives, 1):
        loaded = " [Full]" if d["loaded"] else ""
        volser = f" volser={d['volser']}" if d["volser"] else ""
        print(f"  {i}. {d['nst']} (DTE={d['dte_index'] if d['dte_index'] is not None else '?'}){loaded}{volser}")

    # Mostrar slots com tape
    print()
    print(">>> Slots com tape:")
    try:
        from .mtx_controller import MTXController
        mtx = MTXController(cfg)
        status = mtx.status()
        for se in status.storage_elements:
            if se.full:
                tag = f" tag={se.volume_tag}" if se.volume_tag else ""
                print(f"  Slot {se.slot:3d}: FULL{tag}")
        if not any(se.full for se in status.storage_elements):
            print("  (nenhuma tape na library)")
            _pause(); return
    except Exception as exc:
        print(f"ERRO ao obter status: {exc}")
        _pause(); return

    # 2. Perguntar destdir
    print()
    output_dir = input("Diretório de destino: ").strip()
    if not output_dir:
        print("Diretório inválido."); _pause(); return
    output_path = Path(output_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    print(f"  Destino: {output_path}")

    # 3. Para cada drive: informar slots para extrair
    print()
    print(">>> Atribuição de slots aos drives:")
    print("    Informe os slots para cada drive (separados por vírgula).")
    print("    Deixe em branco para pular um drive.")
    print()

    drive_slots_map: dict[str, list[int]] = {}
    for i, d in enumerate(drives, 1):
        nst = d["nst"]
        slots_input = input(f"  {i}. {nst} - slots para extrair: ").strip()
        if not slots_input:
            print(f"    {nst} pulado.")
            continue
        slots = _parse_int_list(slots_input)
        if not slots:
            print(f"    {nst} sem slots válidos. Pulando.")
            continue
        drive_slots_map[nst] = slots
        print(f"    {nst}: slots {slots}")

    if not drive_slots_map:
        print("\nNenhum drive selecionado para extração.")
        _pause(); return

    # Resumo
    print()
    print(">>> Resumo da extração:")
    total_slots = sum(len(s) for s in drive_slots_map.values())
    print(f"  Drives: {len(drive_slots_map)}")
    print(f"  Slots totais: {total_slots}")
    print(f"  Destino: {output_path}")
    for nst, slots in drive_slots_map.items():
        print(f"    {nst}: slots {slots}")

    # Confirmação
    try:
        ans = input(f"\nConfirma extração de {total_slots} tape(s) via dd? (s/N): "
                    ).strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("s", "sim", "y", "yes"):
        print("Extração cancelada.")
        _pause(); return

    # 4. Executar extração multi-drive paralelo (dd)
    try:
        result = executor.extract_slots_multidrive(drive_slots_map, output_path)
        print()
        print("=" * 60)
        print("  Resumo da extração (dd)")
        print("=" * 60)
        print(f"  Batch ID:    {result['batch_id']}")
        print(f"  Status:      {result['status']}")
        print(f"  Total files: {result['total_files']}")
        print(f"  Total bytes: {result['total_bytes'] / (1024**2):.1f} MB")
        print()
        print("  Manifestos por drive:")
        for nst, manifests in result["per_drive"].items():
            print(f"  [{nst}]")
            for m in manifests:
                icon = "OK" if m["status"] == "ok" else "!!"
                print(f"    [{icon}] {m['volser']:<16} "
                      f"({len(m['files'])} arq) {m['message']}")
        print()
        print(f"  Batch salvo em: {output_path / (result['batch_id'] + '.json')}")
        print("=" * 60)
    except Exception as exc:
        print(f"ERRO durante extração: {exc}")
    _pause()


def cmd_erase_tape(cfg: Config) -> None:
    """Apagar cartucho (mt erase)."""
    from .executor import Executor
    from .mtx_controller import MTXController
    _print_header("Apagar Cartucho (mt erase)")
    print("ATENÇÃO: Esta operação apaga TODOS os dados da tape.")
    print("O processo carrega a tape, executa mt rewind + mt erase,")
    print("e descarrega via mtx unload (sem mt offline).")
    print()

    # Listar drives disponíveis
    print(">>> Drives disponíveis:")
    try:
        executor = Executor(cfg)
        drives = executor.list_available_drives()
    except Exception as exc:
        print(f"ERRO ao listar drives: {exc}")
        _pause(); return

    if not drives:
        print("  (nenhum /dev/nst* encontrado)")
        _pause(); return

    for i, d in enumerate(drives, 1):
        loaded = " [Full]" if d["loaded"] else ""
        print(f"  {i}. {d['nst']} (DTE={d['dte_index'] if d['dte_index'] is not None else '?'}){loaded}")

    # Selecionar drive
    try:
        dev_idx_input = input(f"\nEscolha o drive (1-{len(drives)}): ").strip()
        dev_idx = int(dev_idx_input) if dev_idx_input else 1
    except ValueError:
        dev_idx = 1
    dev_idx = max(1, min(dev_idx, len(drives)))
    selected = drives[dev_idx - 1]
    nst_device = selected["nst"]
    dte_index = selected["dte_index"]

    # Lista slots com tape
    print()
    print(">>> Slots com tape:")
    mtx = MTXController(cfg)
    try:
        status = mtx.status()
        for se in status.storage_elements:
            if se.full:
                tag = f" tag={se.volume_tag}" if se.volume_tag else ""
                print(f"  Slot {se.slot:3d}: FULL{tag}")
    except Exception as exc:
        print(f"ERRO ao obter status: {exc}")
        _pause(); return

    slot_input = input("\nSlot da tape a apagar: ").strip()
    try:
        slot = int(slot_input)
    except ValueError:
        print("Slot inválido."); _pause(); return

    # Dupla confirmação
    try:
        ans = input(f"CONFIRMA apagar a tape no slot {slot} usando {nst_device}? "
                    f"Todos os dados serão perdidos. (digite 'APAGAR'): ").strip()
    except EOFError:
        ans = ""
    if ans != "APAGAR":
        print("Operação cancelada.")
        _pause(); return

    try:
        executor = Executor(cfg)
        ok = executor.erase_tape(slot, nst_device=nst_device, dte_index=dte_index)
        if ok:
            print("\n>>> Tape apagada com sucesso.")
        else:
            print("\n>>> Falha ao apagar a tape. Verifique o log.")
    except Exception as exc:
        print(f"ERRO: {exc}")
    _pause()


def cmd_drive_status(cfg: Config) -> None:
    """Status do drive (mt status completo + oferece descarregar)."""
    from .executor import Executor
    _print_header("Status do Drive")

    # Listar drives disponíveis
    print(">>> Drives disponíveis:")
    try:
        executor = Executor(cfg)
        drives = executor.list_available_drives()
    except Exception as exc:
        print(f"ERRO ao listar drives: {exc}")
        _pause(); return

    if not drives:
        print("  (nenhum /dev/nst* encontrado)")
        _pause(); return

    for i, d in enumerate(drives, 1):
        loaded = " [Full]" if d["loaded"] else ""
        volser = f" volser={d['volser']}" if d["volser"] else ""
        print(f"  {i}. {d['nst']} (DTE={d['dte_index'] if d['dte_index'] is not None else '?'}){loaded}{volser}")

    # Selecionar drive
    try:
        dev_idx_input = input(f"\nEscolha o drive (1-{len(drives)}): ").strip()
        dev_idx = int(dev_idx_input) if dev_idx_input else 1
    except ValueError:
        dev_idx = 1
    dev_idx = max(1, min(dev_idx, len(drives)))
    selected = drives[dev_idx - 1]
    nst_device = selected["nst"]
    dte_index = selected["dte_index"]

    try:
        result = executor.drive_status(nst_device=nst_device, dte_index=dte_index)
        print()
        print(f"  Changer:       {result.get('changer') or '(não resolvido)'}")
        print(f"  Drive device:  {result.get('drive_device') or '(não resolvido)'}")
        print(f"  DTE mtx index: {result.get('dte_index')}")
        print()
        mtx_status = result.get("mtx_status")
        if mtx_status:
            print("  mtx status (DTE):")
            print(f"    status:      {mtx_status.get('status')}")
            print(f"    volume_tag:  {mtx_status.get('volume_tag') or '(nenhum)'}")
            print(f"    loaded_slot: {mtx_status.get('loaded_slot') or '(desconhecido)'}")
        else:
            print("  mtx status: (indisponível)")
        print()
        if result.get("loaded") and result.get("mt_status"):
            print(f"  mt -f {nst_device} status:")
            for line in result["mt_status"].splitlines():
                print(f"    {line}")
        elif not result.get("loaded"):
            print("  mt status: (drive vazio, sem tape)")
        print()

        # Oferece descarregar se há tape
        if result.get("loaded"):
            print("  >>> Há tape carregada no drive.")
            try:
                ans = input("  Descarregar agora? (mtx unload, sem mt offline) (s/N): "
                            ).strip().lower()
            except EOFError:
                ans = "n"
            if ans in ("s", "sim", "y", "yes"):
                ok = executor.unload_drive(nst_device=nst_device, dte_index=dte_index)
                if ok:
                    print("  >>> Tape descarregada com sucesso.")
                else:
                    print("  >>> Falha ao descarregar. Verifique o log.")
        else:
            print("  >>> Drive vazio.")
    except Exception as exc:
        print(f"ERRO: {exc}")
    _pause()


# ---------------------------------------------------------------------------
# Menu (11 opções + Sair)
# ---------------------------------------------------------------------------
MENU_ITEMS = [
    ("1", "Status da Tape Library", cmd_status),
    ("2", "Inventário", cmd_inventory),
    ("3", "Listar drives disponíveis", cmd_list_drives),
    ("4", "Criar plano de gravação", cmd_create_plan),
    ("5", "Validar plano", cmd_validate_plan),
    ("6", "Executar plano (multi-drive paralelo)", cmd_run_plan),
    ("7", "Verificar progresso", cmd_progress),
    ("8", "Retomar execução", cmd_resume),
    ("9", "Extrair tapes para disco (multi-drive paralelo, dd)", cmd_extract),
    ("10", "Apagar cartucho (mt erase)", cmd_erase_tape),
    ("11", "Status do drive", cmd_drive_status),
    ("0", "Sair", None),
]


def _show_menu() -> str:
    print("\n" + "#" * 60)
    print("#  Tape Manager - Menu Interativo (11 opções)")
    print("#" * 60)
    for key, label, _ in MENU_ITEMS:
        print(f"  {key:>2}. {label}")
    return input("\nEscolha: ").strip()


def run_menu(cfg: Config) -> None:
    while True:
        choice = _show_menu()
        if choice == "0":
            print("Saindo.")
            return
        action = next((fn for k, _, fn in MENU_ITEMS if k == choice), None)
        if action is None:
            print("Opção inválida.")
            continue
        try:
            action(cfg)
        except KeyboardInterrupt:
            print("\nInterrompido pelo operador.")
        except Exception as exc:
            print(f"ERRO: {exc}")


# ---------------------------------------------------------------------------
# Modo não-interativo
# ---------------------------------------------------------------------------
def run_command(cfg: Config, command: str, args: argparse.Namespace) -> int:
    log = get_logger()
    if command == "status":
        cmd_status(cfg)
    elif command == "inventory":
        cmd_inventory(cfg)
    elif command == "list_drives":
        cmd_list_drives(cfg)
    elif command == "create_plan":
        if not (args.survey_dir and args.cartridge and args.format):
            print("create_plan requer --survey-dir --cartridge --format")
            return 2
        from .planner import Planner
        plan = Planner(cfg).create_plan(args.survey_dir, args.cartridge,
                                         args.format)
        log.info("Plano: %s", plan.plan_file)
    elif command == "run_plan":
        if not args.plan_file:
            print("run_plan requer --plan-file")
            return 2
        from .executor import Executor
        from .models import SurveyPlan
        json_path = _resolve_plan_json(args.plan_file)
        if not json_path:
            print(f"ERRO: plan.json não encontrado junto a {args.plan_file}")
            return 2
        plan = SurveyPlan.from_json(json_path)
        Executor(cfg).execute_plan(plan)
    elif command == "extract":
        if not (args.slots and args.output_dir):
            print("extract requer --slots --output-dir")
            return 2
        from .executor import Executor
        # No modo non-interactive, usa o primeiro drive disponível
        executor = Executor(cfg)
        drives = executor.list_available_drives()
        if not drives:
            print("ERRO: nenhum drive disponível")
            return 2
        nst = drives[0]["nst"]
        slots = [int(s.strip()) for s in args.slots.split(",") if s.strip()]
        executor.extract_slots_multidrive({nst: slots}, args.output_dir)
    elif command == "erase":
        if not args.slots:
            print("erase requer --slots (um slot)")
            return 2
        from .executor import Executor
        executor = Executor(cfg)
        drives = executor.list_available_drives()
        if not drives:
            print("ERRO: nenhum drive disponível")
            return 2
        nst = drives[0]["nst"]
        dte = drives[0]["dte_index"]
        slots = [int(s.strip()) for s in args.slots.split(",") if s.strip()]
        for slot in slots:
            executor.erase_tape(slot, nst_device=nst, dte_index=dte)
    elif command == "drive_status":
        cmd_drive_status(cfg)
    else:
        print(f"Comando desconhecido: {command}")
        return 2
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    # Pré-processa argv: converte "-mhvtl" (dash único) para "--mhvtl"
    # argparse interpreta -mhvtl como -m -h -v -t -l (short flags),
    # então precisamos normalizar antes do parsing.
    if argv is None:
        argv = list(sys.argv[1:])
    argv = ["--mhvtl" if arg == "-mhvtl" else arg for arg in argv]

    args = _build_parser().parse_args(argv)
    try:
        cfg = Config.from_yaml(args.config, mhvtl_mode=args.mhvtl)
    except Exception as exc:
        print(f"ERRO ao carregar configuração: {exc}")
        return 1
    setup_logging(cfg)
    log = get_logger()
    mode_str = "mhvtl" if cfg.mhvtl_mode else "físico"
    log.info("Tape Manager iniciado (config=%s, modo=%s)", args.config, mode_str)
    if cfg.mhvtl_mode:
        print(f"  [Modo mhvtl ativado] Cartuchos virtuais (JRT01, dummy) disponíveis.")
    else:
        print(f"  [Modo físico] Apenas drives físicos reais. Cartuchos virtuais ocultos.")

    if args.command:
        return run_command(cfg, args.command, args)
    run_menu(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
