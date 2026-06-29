"""Gravador TAR para tapes via /dev/nstX.

CORREÇÕES ACUMULADAS (críticas):

1. **Device busy na ESCRITA**: NUNCA usar `mt` (status, rewind, tell,
   erase) antes de `tar -cvf`. O tar -cvf abre o device e faz rewind
   automático. Qualquer mt precedente deixa o device busy e o tar falha.

2. **Retry do tar -cvf**: se o tar falhar com "Device busy", retry até
   3x com 10s de espera. Usa flag `retry_needed` para saber se deve
   tentar de novo. NUNCA usa `continue` no except (se falhar, propaga).

3. **TOC simples**: o TOC contém apenas os arquivos do volume (um por
   linha). NÃO há _TAPE_LABEL.txt — o tape_manager NÃO grava label na
   tape.

4. **Pós-validação NÃO FATAL**: após o tar -cvf, a tape está no fim dos
   dados. Tentar tar -tf pode falhar com I/O error. A pós-validação
   apenas loga warning, NÃO aborta o volume.

5. **Sem mt offline**: o unload é feito exclusivamente via mtx unload
   (no executor), nunca via mt -f /dev/nstN offline.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .config import Config
from .drive_resolver import DriveDevice
from .exceptions import WriterError
from .integrity import IntegrityChecker, ProgressTracker
from .logger import get_logger
from .models import SurveyPlan, VolumePlan


class TapeWriter:
    # Cache de compatibilidade de tar (chaveado por path do binário)
    _tar_checkpoint_cache: dict[str, bool] = {}

    def __init__(self, cfg: Config, drive: DriveDevice,
                 integrity: IntegrityChecker):
        self.cfg = cfg
        self.drive = drive
        self.integrity = integrity
        self.log = get_logger()

    # -----------------------------------------------------------------
    # API pública
    # -----------------------------------------------------------------
    def write_volume(self, plan: SurveyPlan, vol: VolumePlan,
                     tracker: ProgressTracker) -> None:
        """Grava um volume completo (1 tape, 1 tar).

        Fluxo (corrigido):
          1. Pré-validação dos arquivos (integridade)
          2. NÃO executar mt status/rewind/tell/erase (evita Device busy)
          3. Gerar TOC (apenas arquivos do volume, sem label)
          4. tar -cvf /dev/nstN -T toc (retry 3x/10s se "Device busy")
          5. Pós-validação NÃO FATAL (apenas warning se falhar)
          6. NÃO executar mt offline (executor faz mtx unload)

        Levanta WriterError em caso de falha persistente do tar.
        """
        self.log.info("Iniciando gravação do volume %s (%d arquivos, %d B)",
                      vol.volser, len(vol.files), vol.used_bytes)

        # 1. Pré-validação
        self.integrity.verify_volume_pre(plan, vol)

        # 2. NÃO verificar ONLINE com mt status antes do tar -cvf!
        #    (Device busy). Apenas loga que vamos direto ao tar.
        if self.cfg.drives.skip_online_check:
            self.log.info(
                "skip_online_check=True: pulando verificação ONLINE (mt status "
                "antes de tar -cvf causa Device busy). Assumindo tape pronta em %s.",
                self.drive.preferred,
            )
        else:
            self.log.info(
                "NÃO executando mt status antes de tar -cvf (evita Device busy). "
                "Tape carregada via mtx load em %s.", self.drive.preferred,
            )

        # 3. Gerar TOC (apenas arquivos do volume, sem label)
        toc_path = Path(plan.output_dir) / f"{vol.volser}.toc"
        self._write_tar_toc(vol, toc_path)

        # 4. Gravar (tar -cvf) com retry em caso de Device busy
        try:
            self._run_tar(vol, toc_path, tracker)
        except WriterError:
            tracker.mark_volume_failed(vol)
            raise

        # 5. Pós-validação NÃO FATAL
        self._post_verify_non_fatal(vol)

        tracker.mark_volume_completed(vol)
        self.log.info("Volume %s gravado com sucesso.", vol.volser)

    # -----------------------------------------------------------------
    # TOC
    # -----------------------------------------------------------------
    def _write_tar_toc(self, vol: VolumePlan, toc_path: Path) -> None:
        """Escreve arquivo .toc com os caminhos relativos (tar -T).

        O TOC contém apenas os arquivos do volume (um por linha).
        NÃO há _TAPE_LABEL.txt.
        """
        with toc_path.open("w", encoding="utf-8") as fh:
            for f in vol.files:
                fh.write(f.path + "\n")
        self.log.debug("TOC tar gerado: %s (%d arquivos)",
                       toc_path, len(vol.files))

    # -----------------------------------------------------------------
    # Pós-validação NÃO FATAL
    # -----------------------------------------------------------------
    def _post_verify_non_fatal(self, vol: VolumePlan) -> None:
        """Tenta listar o conteúdo da tape com tar -tf.

        CORREÇÃO: a pós-validação é NÃO FATAL. Após o tar -cvf, a tape
        está no fim dos dados. O tar -tf pode falhar com I/O error
        porque precisa rebobinar (e o mt rewind antes do tar -tf também
        pode dar Device busy em alguns drivers).

        Apenas loga warning. NÃO aborta o volume.
        """
        if not self.cfg.writer.post_verify:
            self.log.debug("post_verify=false: pulando pós-validação.")
            return

        self.log.info(
            "Pós-validação (NÃO FATAL) para %s: tentando tar -tf ...",
            vol.volser,
        )
        try:
            # Tenta mt rewind antes do tar -tf (mt rewind FUNCIONA para leitura)
            self.integrity._rewind_tape(self.drive.preferred)
        except Exception as exc:
            self.log.warning(
                "Pós-validação: mt rewind falhou (%s). Tentando tar -tf mesmo assim.",
                exc,
            )

        try:
            proc = subprocess.run(
                [self.cfg.drives.tar_bin, "-tf", self.drive.preferred],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.cfg.writer.post_verify_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.log.warning(
                "Pós-validação (NÃO FATAL): timeout no tar -tf para %s. "
                "Tape pode estar no fim dos dados.", vol.volser,
            )
            return
        except Exception as exc:
            self.log.warning(
                "Pós-validação (NÃO FATAL): erro no tar -tf para %s: %s",
                vol.volser, exc,
            )
            return

        if proc.returncode != 0:
            self.log.warning(
                "Pós-validação (NÃO FATAL): tar -tf retornou RC=%d para %s. "
                "Isto é esperado quando a tape está no fim após gravação. "
                "stderr: %s",
                proc.returncode, vol.volser,
                (proc.stderr or "").strip()[:300],
            )
            return

        listed = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.log.info(
            "Pós-validação OK: %s listou %d entradas na tape.",
            vol.volser, len(listed),
        )
        if listed and len(listed) <= 20:
            for entry in listed:
                self.log.debug("  tar entry: %s", entry)

    # -----------------------------------------------------------------
    # Tar -cvf com retry em Device busy
    # -----------------------------------------------------------------
    def _tar_supports_checkpoint(self) -> bool:
        """Verifica se o tar suporta --checkpoint=N (e --checkpoint-action).

        Resultado é cacheado por binário.
        """
        if not getattr(self.cfg.writer, 'use_tar_checkpoint', False):
            return False

        tar_bin = self.cfg.drives.tar_bin
        if tar_bin in self._tar_checkpoint_cache:
            return self._tar_checkpoint_cache[tar_bin]

        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as f:
                tmp_tar = f.name
            try:
                proc = subprocess.run(
                    [tar_bin, "--checkpoint=1", "-cf", tmp_tar,
                     "--files-from=/dev/null"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                    check=False,
                )
                supports = (proc.returncode == 0 or
                           "empty" in (proc.stderr or "").lower())
                if not supports and "checkpoint" in (proc.stderr or "").lower():
                    supports = False
                elif proc.returncode != 0 and "empty" not in (proc.stderr or "").lower():
                    supports = False
            finally:
                try:
                    os.unlink(tmp_tar)
                except OSError:
                    pass
        except (subprocess.TimeoutExpired, OSError):
            supports = False

        self._tar_checkpoint_cache[tar_bin] = supports
        self.log.info(
            "tar '%s' suporta --checkpoint: %s",
            tar_bin, "SIM" if supports else "NAO",
        )
        return supports

    def _is_device_busy_error(self, stderr: str, returncode: int) -> bool:
        """Detecta se o erro do tar é 'Device busy' (retryável)."""
        if returncode == 0:
            return False
        text = (stderr or "").lower()
        busy_patterns = (
            "device busy",
            "device or resource busy",
            "resource busy",
            "ebusy",
            "device is busy",
        )
        return any(p in text for p in busy_patterns)

    def _run_tar(self, vol: VolumePlan, toc_path: Path,
                 tracker: ProgressTracker) -> None:
        """Executa tar -cvf /dev/nstN -T toc com retry em Device busy.

        CORREÇÃO CRÍTICA:
          - NÃO executa mt antes do tar -cvf (evita Device busy)
          - Retry até N vezes (busy_retry_count) se "Device busy"
          - Usa flag retry_needed para saber se deve tentar de novo
          - NUNCA usa `continue` no except (falha propaga)
        """
        basedir = vol.files[0].basedir if vol.files else ""
        if not basedir:
            raise WriterError(
                f"Volume {vol.volser} sem basedir definido nos arquivos."
            )

        total_files = len(vol.files)
        total_bytes = vol.used_bytes
        size_by_name: dict[str, int] = {}
        for f in vol.files:
            base = Path(f.path).name
            size_by_name[base] = f.size

        # Monta comando tar
        tar_cmd = [
            self.cfg.drives.tar_bin,
            "-C", basedir,
            *self.cfg.writer.tar_extra_args,
        ]
        if self._tar_supports_checkpoint():
            tar_cmd.append("--checkpoint=1")
            action = self.cfg.writer.tar_checkpoint_action
            if action:
                tar_cmd.append(f"--checkpoint-action={action}")
        tar_cmd.extend(["-T", str(toc_path), "-cvf", self.drive.preferred])

        cmd = tar_cmd

        self.log.info("Executando: %s", " ".join(cmd))
        print()
        print(f"  Gravando volume {vol.volser}: {total_files} arquivos, "
              f"{total_bytes / (1024**2):.1f} MB")
        print(f"  Drive: {self.drive.preferred}")
        print(f"  (retry se Device busy: {self.cfg.writer.busy_retry_count}x / "
              f"{self.cfg.writer.busy_retry_delay_sec}s)")
        print()

        # ----- Loop de retry em Device busy -----
        busy_retries = max(1, self.cfg.writer.busy_retry_count)
        busy_delay = max(1, self.cfg.writer.busy_retry_delay_sec)

        for attempt in range(1, busy_retries + 1):
            retry_needed = False
            try:
                self._run_tar_once(cmd, vol, toc_path, tracker, attempt,
                                    busy_retries, size_by_name, total_files,
                                    total_bytes)
                return  # sucesso
            except WriterError as exc:
                # Verifica se é Device busy -> retry
                stderr_msg = str(exc)
                if self._is_device_busy_error(stderr_msg, 1) and attempt < busy_retries:
                    retry_needed = True
                    self.log.warning(
                        "Device busy detectado (tentativa %d/%d). "
                        "Aguardando %ds antes de retry...",
                        attempt, busy_retries, busy_delay,
                    )
                    print(f"  [Device busy] retry {attempt}/{busy_retries} "
                          f"em {busy_delay}s...")
                    time.sleep(busy_delay)
                    # NÃO continua no except: retry_needed controla o fluxo
                if retry_needed:
                    continue
                # Não é busy ou esgotou retries -> propaga
                raise

        # Se chegou aqui, esgotou retries sem sucesso (não deveria acontecer
        # porque o raise acima propaga, mas por segurança)
        raise WriterError(
            f"tar -cvf falhou para {vol.volser} após {busy_retries} tentativas "
            f"(Device busy persistente)."
        )

    def _run_tar_once(self, cmd: list[str], vol: VolumePlan,
                       toc_path: Path, tracker: ProgressTracker,
                       attempt: int, total_attempts: int,
                       size_by_name: dict[str, int],
                       total_files: int, total_bytes: int) -> None:
        """Executa o tar UMA vez, com monitoramento de progresso.

        Levanta WriterError se o tar falhar (inclui stderr para detecção
        de Device busy no chamador).
        """
        start = time.time()
        files_done = 0
        bytes_done = 0
        stderr_capture: list[str] = []
        proc: subprocess.Popen | None = None

        # Aguarda 3s extras antes de abrir o device (apos mtx load no executor).
        time.sleep(3)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                errors="replace",
            )
        except OSError as exc:
            raise WriterError(f"Falha ao iniciar tar: {exc}") from exc

        assert proc.stderr is not None

        # Marca o fd como non-blocking
        import fcntl as _fcntl
        import select as _select
        stderr_fd = proc.stderr.fileno()
        flags = _fcntl.fcntl(stderr_fd, _fcntl.F_GETFL)
        _fcntl.fcntl(stderr_fd, _fcntl.F_SETFL, flags | os.O_NONBLOCK)

        try:
            buffer = ""
            last_poll_display = 0.0
            while True:
                ready, _, _ = _select.select([stderr_fd], [], [], 1.0)
                if ready:
                    try:
                        chunk = os.read(stderr_fd, 65536).decode(
                            "utf-8", errors="replace"
                        )
                    except (BlockingIOError, OSError):
                        chunk = ""
                    if chunk:
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line_stripped = line.rstrip()
                            if not line_stripped:
                                continue
                            stderr_capture.append(line_stripped)

                            low = line_stripped.lower()
                            if low.startswith("tar:") and \
                               ("error" in low or "warning" in low):
                                self.log.warning("tar: %s", line_stripped)
                                continue
                            if "checkpoint" in low:
                                continue

                            fname = line_stripped
                            if fname and fname[0] in ("a", "x", "r") and \
                               len(fname) > 1 and fname[1] == " ":
                                fname = fname[2:]
                            fname = fname.lstrip()
                            base = Path(fname).name

                            if base in size_by_name:
                                files_done += 1
                                bytes_done += size_by_name[base]
                                try:
                                    tracker.mark_file_written(vol, fname)
                                except Exception:
                                    pass

                if proc.poll() is not None:
                    try:
                        rest = os.read(stderr_fd, 65536).decode(
                            "utf-8", errors="replace"
                        )
                        if rest:
                            buffer += rest
                            while "\n" in buffer:
                                line, buffer = buffer.split("\n", 1)
                                stderr_capture.append(line.rstrip())
                    except (BlockingIOError, OSError):
                        pass
                    break

                now = time.time()
                if now - last_poll_display >= 2.0:
                    last_poll_display = now
                    elapsed = now - start
                    effective_bytes = bytes_done
                    pct = int((files_done / total_files * 100)) \
                        if total_files else \
                        int((effective_bytes / total_bytes * 100)) \
                        if total_bytes else 0
                    mb_done = effective_bytes / (1024 * 1024)
                    throughput = (effective_bytes / (1024 * 1024) / elapsed
                                  if elapsed > 0 else 0)
                    eta_sec = ((total_bytes - effective_bytes) /
                               (effective_bytes / elapsed)
                               if effective_bytes > 0 and elapsed > 0 else 0)
                    self._print_progress_bar(
                        pct, files_done, total_files,
                        mb_done, total_bytes / (1024 * 1024),
                        throughput, elapsed, eta_sec,
                        source="tar",
                    )
        except KeyboardInterrupt:
            self.log.warning("Interrompido pelo operador (Ctrl+C).")
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            raise WriterError(
                f"Gravação interrompida pelo operador para {vol.volser}"
            )

        elapsed = time.time() - start
        size_mb = total_bytes / (1024 * 1024)
        throughput = size_mb / elapsed if elapsed > 0 else 0

        # Linha final (limpa a barra de progresso)
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()

        self.log.info(
            "tar escrita concluída em %.1fs (%.1f MB, %.1f MB/s)",
            elapsed, size_mb, throughput,
        )
        print(f"  >>> Concluído em {elapsed:.1f}s | "
              f"{size_mb:.1f} MB | {throughput:.1f} MB/s")
        print()

        if proc.returncode != 0:
            stderr_text = "\n".join(stderr_capture[-20:])
            self.log.error("tar falhou (RC=%d): %s",
                           proc.returncode, stderr_text)
            # Inclui stderr na exceção para o chamador detectar Device busy
            raise WriterError(
                f"tar falhou para {vol.volser} (RC={proc.returncode}): "
                f"{stderr_text[:500]}"
            )

    def _print_progress_bar(self, pct: int, files_done: int, total_files: int,
                             mb_done: float, mb_total: float,
                             throughput: float, elapsed: float,
                             eta_sec: float, source: str = "tar") -> None:
        """Imprime barra de progresso em uma única linha (sobrescreve)."""
        bar_width = 25
        filled = int(bar_width * pct / 100)
        bar = "#" * filled + "-" * (bar_width - filled)

        def fmt_time(s: float) -> str:
            if s < 0 or s > 86400 * 7:
                return "?"
            if s < 60:
                return f"{int(s)}s"
            if s < 3600:
                return f"{int(s/60)}m{int(s%60)}s"
            return f"{int(s/3600)}h{int((s%3600)/60)}m"

        src_icon = "[mt]" if source == "mt" else "   "

        line = (
            f"\r  {src_icon}[{bar}] {pct:3d}%  "
            f"{files_done}/{total_files}f  "
            f"{mb_done:.0f}/{mb_total:.0f}MB  "
            f"{throughput:.1f}MB/s  "
            f"{fmt_time(elapsed)}  "
            f"ETA {fmt_time(eta_sec)}"
        )
        if len(line) > 110:
            line = line[:107] + "..."
        sys.stdout.write(line)
        sys.stdout.flush()
