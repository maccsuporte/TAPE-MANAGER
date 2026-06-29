"""Planejador de cópia.

Regras obrigatórias (preservadas e reforçadas):
  - Capacidade real por cartucho/formato vem da tabela centralizada.
  - Algoritmo FFD (First Fit Decreasing): ordena arquivos por tamanho
    decrescente e preenche cada tape ao máximo sem estourar capacidade.
  - Arquivo maior que a capacidade da tape NUNCA é dividido -> é listado
    em `skipped_files` e o plano fica inválido até o operador resolver.
  - Nenhum arquivo é gravado parcialmente.
  - Volsers são auto-gerados como `vol{index:03d}` (vol001, vol002, ...).
    Não há mais input de labels customizados pelo operador.
  - Multi-cartucho automático via `_find_smallest_cartridge_for_size`:
    quando o operador escolhe cartridge="auto", o planner seleciona o
    menor cartucho que comporta cada conjunto de arquivos.

  - Plano é materializado em disco em 2 formatos:
      plan.<cartucho>.<formato>.txt   (uma linha por volume, formato legado)
      volXXXXX_YYYYGiB.lst            (TOC por volume, formato size:basedir:file)
    E persistido como plan.json (primário, com estado completo para
    validação/execução/retomada, incluindo drive_assignments e
    slot_assignments para multi-drive paralelo).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Config, GIB
from .exceptions import PlanValidationError
from .logger import get_logger
from .models import FileEntry, SurveyPlan, VolumePlan


class Planner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()

    # -----------------------------------------------------------------
    # API pública
    # -----------------------------------------------------------------
    def create_plan(self, survey_dir: str | Path, cartridge: str,
                    fmt: str, output_dir: str | Path | None = None) -> SurveyPlan:
        """Cria um plano de gravação.

        Parâmetros:
          survey_dir  : diretório do survey (recursivamente varrido)
          cartridge   : tipo de cartucho (ex: JBE07) ou "auto" para
                         seleção automática multi-cartucho
          fmt         : "tar" | "ltfs"
          output_dir  : diretório de saída (default: cfg.default_output_dir/survey_name)
        """
        survey_dir = Path(survey_dir).resolve()
        fmt = fmt.lower()

        # Normaliza cartridge ("auto" é mantido para multi-cartucho)
        is_auto = cartridge.lower() == "auto"
        if not is_auto:
            matched = next(
                (k for k in self.cfg.cartridges if k.lower() == cartridge.lower()),
                None,
            )
            if matched is None:
                raise PlanValidationError(
                    f"Cartucho '{cartridge}' não suportado. Disponíveis: "
                    f"{', '.join(sorted(self.cfg.cartridges))} ou 'auto'."
                )
            cartridge = matched
        self._validate_inputs(survey_dir, cartridge, fmt, is_auto)

        if output_dir is None:
            output_dir = Path(self.cfg.default_output_dir) / survey_dir.name
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        basedir = survey_dir.parent
        survey_name = survey_dir.name

        # Capacidade base (para modo não-auto)
        if is_auto:
            max_bytes = 0  # será determinado por volume via _find_smallest_cartridge_for_size
        else:
            max_bytes = self.cfg.cartridge_capacity_bytes(cartridge, fmt)
            max_bytes = max(0, max_bytes - self.cfg.planner.safety_margin_bytes)

        self.log.info(
            "Criando plano: survey=%s cartucho=%s formato=%s %s",
            survey_dir, cartridge, fmt,
            f"(multi-cartucho automático)" if is_auto else f"(capacidade={max_bytes // GIB} GiB)",
        )

        files = self._enumerate_files(survey_dir, basedir, survey_name)
        self.log.info("Total de arquivos encontrados: %d", len(files))

        # Skipped: arquivos maiores que o maior cartucho disponível
        max_capacity_any = max(
            self.cfg.cartridge_capacity_bytes(c, fmt)
            for c in self.cfg.cartridges
            if self.cfg.cartridges[c].get(fmt) is not None
        )
        max_capacity_any = max(0, max_capacity_any - self.cfg.planner.safety_margin_bytes)

        skipped: list[dict] = []
        for f in files:
            if f.size > max_capacity_any:
                skipped.append({
                    "path": f.path,
                    "size": f.size,
                    "reason": f"arquivo ({f.size} B) maior que a maior capacidade "
                              f"disponível ({max_capacity_any} B)",
                })
                self.log.warning("Arquivo maior que qualquer tape será ignorado: %s (%d B)",
                                 f.path, f.size)

        viable = [f for f in files if f.size <= max_capacity_any]
        volumes = self._pack_ffd(
            viable, max_bytes, output_dir, cartridge, fmt,
            is_auto=is_auto,
        )

        plan_file = output_dir / self.cfg.planner.plan_filename_template.format(
            cartridge=cartridge, format=fmt,
        )
        plan = SurveyPlan(
            survey_dir=str(survey_dir),
            basedir=str(basedir),
            survey_name=survey_name,
            cartridge=cartridge,
            fmt=fmt,
            output_dir=str(output_dir),
            plan_file=str(plan_file),
            volumes=volumes,
            total_files=len(files),
            total_bytes=sum(f.size for f in files),
            skipped_files=skipped,
        )

        # Materializa em disco (formato compatível com scripts originais)
        self._write_plan_files(plan)
        # Persiste JSON completo para validação/execução/retomada.
        json_path = output_dir / "plan.json"
        try:
            plan.to_json(json_path)
            self.log.info("Plano JSON persistido: %s", json_path)
        except Exception as exc:
            self.log.error("Falha ao persistir %s: %s", json_path, exc)
            raise
        self.log.info(
            "Plano criado: %d volumes, %d arquivos, %d saltos.",
            len(volumes), len(files), len(skipped),
        )
        self.log.info("  Texto (legado): %s", plan_file)
        self.log.info("  JSON (primário): %s", json_path)
        return plan

    def validate_plan(self, plan: SurveyPlan) -> None:
        """Validações de integridade do plano antes da gravação."""
        if not plan.volumes:
            raise PlanValidationError("Plano não contém nenhum volume.")
        if plan.skipped_files:
            raise PlanValidationError(
                f"Plano contém {len(plan.skipped_files)} arquivo(s) maior(es) "
                f"que a capacidade da tape. Resolva antes de gravar."
            )

        for vol in plan.volumes:
            # Determina capacidade aplicável a este volume (multi-cartucho)
            cart = vol.effective_cartridge or plan.cartridge
            if cart and cart.lower() != "auto":
                max_bytes = self.cfg.cartridge_capacity_bytes(cart, plan.fmt)
                max_bytes = max(0, max_bytes - self.cfg.planner.safety_margin_bytes)
                if vol.used_bytes > max_bytes:
                    raise PlanValidationError(
                        f"Volume {vol.volser} (cartucho={cart}) excede capacidade: "
                        f"{vol.used_bytes} > {max_bytes} bytes"
                    )
            # Pré-validação: arquivos existem e têm tamanho esperado
            for f in vol.files:
                full = Path(plan.basedir) / f.path
                if not full.is_file():
                    raise PlanValidationError(
                        f"Arquivo não encontrado: {full}"
                    )
                actual = full.stat().st_size
                if actual != f.size:
                    raise PlanValidationError(
                        f"Tamanho diverge para {full}: esperado={f.size} "
                        f"atual={actual}"
                    )
                f.verified_pre = True
        self.log.info("Plano validado: %d volumes OK.", len(plan.volumes))

    # -----------------------------------------------------------------
    # Multi-cartucho: seleciona o menor cartucho que comporta um tamanho
    # -----------------------------------------------------------------
    def _find_smallest_cartridge_for_size(self, size_bytes: int,
                                          fmt: str,
                                          exclude: str | None = None) -> str | None:
        """Retorna o nome do menor cartucho cuja capacidade (após margem
        de segurança) comporta `size_bytes` no formato `fmt`.

        Args:
          exclude: nome do cartucho a excluir (ex: o cartucho principal)
        Retorna None se nenhum cartucho comportar o tamanho.
        """
        best: tuple[str, int] | None = None
        for name in sorted(self.cfg.cartridges):
            if exclude and name.upper() == exclude.upper():
                continue
            caps = self.cfg.cartridges[name]
            gib = caps.get(fmt)
            if gib is None:
                continue
            cap_bytes = int(gib) * GIB - self.cfg.planner.safety_margin_bytes
            if cap_bytes <= 0:
                continue
            if size_bytes <= cap_bytes:
                if best is None or cap_bytes < best[1]:
                    best = (name, cap_bytes)
        return best[0] if best else None

    # -----------------------------------------------------------------
    # Internos
    # -----------------------------------------------------------------
    def _validate_inputs(self, survey_dir: Path, cartridge: str,
                         fmt: str, is_auto: bool) -> None:
        if not survey_dir.is_dir():
            raise PlanValidationError(f"Não é um diretório: {survey_dir}")
        if not os.access(survey_dir, os.R_OK | os.X_OK):
            raise PlanValidationError(
                f"Sem permissão de leitura/execução no diretório: {survey_dir}"
            )
        if fmt not in ("tar", "ltfs"):
            raise PlanValidationError(
                f"Formato '{fmt}' não suportado. Use 'tar' ou 'ltfs'."
            )
        if not is_auto and cartridge not in self.cfg.cartridges:
            raise PlanValidationError(
                f"Cartucho '{cartridge}' não suportado. Disponíveis: "
                f"{', '.join(sorted(self.cfg.cartridges))} ou 'auto'."
            )

    def _enumerate_files(self, survey_dir: Path, basedir: Path,
                         survey_name: str) -> list[FileEntry]:
        """Varre survey_dir recursivamente, calcula tamanho real."""
        files: list[FileEntry] = []
        dirs_visited = 0
        errors = 0
        skipped_zero_size = 0

        self.log.info("Varrendo arquivos em %s ...", survey_dir)
        for root, _dirs, names in os.walk(survey_dir, onerror=self._on_walk_error):
            dirs_visited += 1
            for name in names:
                full = Path(root) / name
                try:
                    st = full.stat()
                except OSError as exc:
                    errors += 1
                    self.log.warning("Ignorando (stat falhou): %s (%s)", full, exc)
                    continue
                if not full.is_file():
                    continue
                if st.st_size == 0:
                    skipped_zero_size += 1
                rel = full.relative_to(basedir).as_posix()
                files.append(FileEntry(path=rel, size=st.st_size, basedir=str(basedir)))

                if len(files) % 1000 == 0:
                    self.log.info("  ... %d arquivos listados (%d dirs visitados)",
                                  len(files), dirs_visited)

        self.log.info(
            "Varredura concluída: %d arquivos válidos, %d diretórios visitados, "
            "%d erros, %d arquivos vazios",
            len(files), dirs_visited, errors, skipped_zero_size,
        )

        if not files:
            self.log.error(
                "NENHUM arquivo encontrado em %s\n"
                "  Verifique:\n"
                "    - O diretório contém arquivos (não apenas subdiretórios vazios)\n"
                "    - O usuário tem permissão de leitura recursiva\n"
                "    - Os arquivos não são sockets/fifos/devices (são ignorados)\n"
                "  Diretórios visitados: %d | Erros: %d | Arquivos vazios: %d",
                survey_dir, dirs_visited, errors, skipped_zero_size,
            )
        return files

    def _on_walk_error(self, exc: OSError) -> None:
        """Callback de erro do os.walk: loga mas não interrompe."""
        self.log.warning("Erro varrendo diretório: %s (%s)", exc.filename, exc.strerror)

    def _pack_ffd(self, files: list[FileEntry], max_bytes: int,
                  output_dir: Path, cartridge: str, fmt: str,
                  is_auto: bool = False) -> list[VolumePlan]:
        """First Fit Decreasing: ordena por tamanho desc e preenche tapes.

        MULTI-CARTUCHO: se um arquivo nao cabe no cartucho principal,
        procura o menor cartucho que o comporte e cria volume separado.
        Funciona tanto em modo 'auto' quanto em modo cartucho especifico.

        Volsers são auto-gerados como `vol{index:03d}`.
        """
        files_sorted = sorted(files, key=lambda f: f.size, reverse=True)
        volumes: list[VolumePlan] = []
        remaining = list(files_sorted)

        vol_index = 0
        while remaining:
            vol_index += 1
            volser = f"vol{vol_index:03d}"

            vol_max = max_bytes if not is_auto else 0
            vol = VolumePlan(
                index=vol_index, volser=volser,
                toc_filename="", toc_path="",
                max_bytes=vol_max, cartridge="",
            )
            leftover: list[FileEntry] = []
            oversized_for_this_vol: list[FileEntry] = []

            for f in remaining:
                if is_auto:
                    vol.add_file(f)
                else:
                    if f.size > max_bytes:
                        # Arquivo maior que o cartucho principal.
                        # Nao vai para este volume; sera processado depois
                        # com cartucho maior.
                        oversized_for_this_vol.append(f)
                        continue
                    if vol.used_bytes + f.size <= max_bytes:
                        vol.add_file(f)
                    else:
                        leftover.append(f)

            # Se ha oversized e o modo NAO é auto, processa cada um
            # com cartucho maior
            if oversized_for_this_vol and not is_auto:
                for big_file in oversized_for_this_vol:
                    alt_cart = self._find_smallest_cartridge_for_size(
                        big_file.size, fmt, exclude=cartridge,
                    )
                    if alt_cart is None:
                        # Nenhum cartucho comporta -> vai para skipped
                        self.log.error(
                            "NENHUM cartucho comporta %s (%.2f GiB). SALTADO.",
                            big_file.path, big_file.size / GIB,
                        )
                        continue
                    # Cria volume separado para o arquivo oversized
                    vol_index += 1
                    big_volser = f"vol{vol_index:03d}"
                    alt_max = self.cfg.cartridge_capacity_bytes(alt_cart, fmt)
                    alt_max = max(0, alt_max - self.cfg.planner.safety_margin_bytes)
                    big_vol = VolumePlan(
                        index=vol_index, volser=big_volser,
                        toc_filename="", toc_path="",
                        max_bytes=alt_max, cartridge=alt_cart,
                    )
                    big_vol.add_file(big_file)
                    toc_name = self.cfg.planner.volume_filename_template.format(
                        index=big_vol.index, used_gib=big_vol.used_gib,
                    )
                    big_vol.toc_filename = toc_name
                    big_vol.toc_path = str(output_dir / toc_name)
                    volumes.append(big_vol)
                    self.log.info(
                        "Volume %s: cartucho=%s (ALTERNATIVO para arquivo maior) "
                        "used=%.2f GiB",
                        big_volser, alt_cart, big_vol.used_bytes / GIB,
                    )

            if is_auto:
                chosen = self._find_smallest_cartridge_for_size(vol.used_bytes, fmt)
                if chosen is None:
                    max_any = max(
                        self.cfg.cartridge_capacity_bytes(c, fmt)
                        for c in self.cfg.cartridges
                        if self.cfg.cartridges[c].get(fmt) is not None
                    )
                    max_any = max(0, max_any - self.cfg.planner.safety_margin_bytes)
                    vol.max_bytes = max_any
                    chosen = self._largest_cartridge(fmt)
                    leftover = list(vol.files)
                    vol.files = []
                    vol.used_bytes = 0
                    for f in leftover:
                        if f.size > max_any:
                            continue
                        if vol.used_bytes + f.size <= max_any:
                            vol.add_file(f)
                    new_leftover = [f for f in leftover if f not in vol.files]
                    leftover = new_leftover
                vol.cartridge = chosen or ""
                if chosen:
                    cap = self.cfg.cartridge_capacity_bytes(chosen, fmt)
                    vol.max_bytes = max(0, cap - self.cfg.planner.safety_margin_bytes)

            # Se o volume tem arquivos, adiciona
            if vol.files:
                toc_name = self.cfg.planner.volume_filename_template.format(
                    index=vol.index, used_gib=vol.used_gib,
                )
                vol.toc_filename = toc_name
                vol.toc_path = str(output_dir / toc_name)
                if not vol.cartridge:
                    vol.cartridge = cartridge if not is_auto else vol.cartridge
                volumes.append(vol)
                self.log.info(
                    "Volume %s: cartucho=%s used=%.2f GiB (%d arqs)",
                    volser, vol.cartridge, vol.used_bytes / GIB, len(vol.files),
                )

            remaining = leftover
        return volumes

    def _largest_cartridge(self, fmt: str) -> str:
        """Retorna o nome do maior cartucho disponível para o formato."""
        best: tuple[str, int] | None = None
        for name, caps in self.cfg.cartridges.items():
            gib = caps.get(fmt)
            if gib is None:
                continue
            cap = int(gib) * GIB
            if best is None or cap > best[1]:
                best = (name, cap)
        return best[0] if best else ""

    def _write_plan_files(self, plan: SurveyPlan) -> None:
        """Escreve:
          - plan.<cart>.<fmt>.txt  (formato: volser:/dev/rmtXX:tocfile.lst)
          - volXXXXX_YYYYGiB.lst   (formato: size:basedir:filename)
        """
        plan_path = Path(plan.plan_file)
        with plan_path.open("w", encoding="utf-8") as fh:
            for vol in plan.volumes:
                fh.write(f"{vol.volser}:/dev/rmtXX:{vol.toc_path}\n")

        for vol in plan.volumes:
            with Path(vol.toc_path).open("w", encoding="utf-8") as fh:
                for f in vol.files:
                    fh.write(f"{f.size}:{f.basedir}:{f.path}\n")
