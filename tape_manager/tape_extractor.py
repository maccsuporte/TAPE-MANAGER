"""Extrator de tapes (leitura via dd — arquivo bruto).

CORREÇÕES ACUMULADAS (críticas):

1. **mt rewind FUNCIONA para leitura**: o busy só acontece quando mt
   precede tar -cvf (escrita). Para leitura, o mt rewind é seguro
   e necessário.

2. **mt setblk 0 antes de dd**: CRÍTICO para ler tapes escritas por
   tar -cvf no mhvtl. Coloca a tape em modo de bloco variável.
   Sem isto, dd retorna "Cannot allocate memory" (ENOMEM) porque
   o driver st tenta alocar buffer para bloco fixo e falha.

3. **Fluxo de EXTRAÇÃO via dd** (arquivo bruto, sem tar -xf):
     a. mtx load slot -> drive
     b. sleep(5)
     c. mt rewind (funciona para leitura)
     d. mt setblk 0 (modo bloco variável)
     e. dd loop: dd if=/dev/nstN of=<destdir>/<volser>_<N>.sgy bs=128k
        - Lê cada segmento (entre file marks) da tape como blob bruto
        - NÃO usa iflag=fullblock (causa ENOMEM no mhvtl)
        - Em modo variável, cada read() retorna um bloco da tape
        - Se arquivo = 0 bytes -> EOF, remover, próximo slot
        - Se dd RC=0 -> .sgy (sucesso)
        - Se dd RC!=0 mas gravou dados -> .err
     f. mtx unload
     g. sleep(10)

4. **Sem tar -xf**: O dd lê o conteúdo bruto da tape e salva direto
   como arquivo .sgy no destino. Não há interpretação do conteúdo.

5. **Manifesto**: gera um TapeManifest por tape extraída, com a lista
   de arquivos extraídos. Agrega em um ExtractionBatch.

6. **SEM label**: o tape_manager NÃO grava _TAPE_LABEL.txt na tape.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .config import Config
from .exceptions import ExtractorError
from .logger import get_logger
from .models import (
    ExtractedFile,
    ExtractionBatch,
    TapeManifest,
)
from .mtx_controller import MTXController


class TapeExtractor:
    """Extrai arquivos de tapes para um diretório de destino via dd (bruto).

    MULTI-DRIVE: cada instância processa UM drive (/dev/nstN) e seus slots.
    Para extração paralela, o Executor cria uma instância por thread.
    """

    def __init__(self, cfg: Config, mtx: MTXController):
        self.cfg = cfg
        self.mtx = mtx
        self.log = get_logger()

    # -----------------------------------------------------------------
    # API pública
    # -----------------------------------------------------------------
    def extract_tape(self, slot: int, drive_index: int,
                     nst_device: str, output_dir: str | Path,
                     volser: str | None = None) -> TapeManifest:
        """Extrai UMA tape (do slot) para output_dir via dd (arquivo bruto).

        Fluxo:
          1. mtx load slot -> drive
          2. sleep(5)
          3. mt rewind (funciona para leitura)
          3b. mt setblk 0 (modo bloco variável — CRÍTICO)
          4. dd loop (bs=128k, sem iflag=fullblock):
             - dd if=/dev/nstN of=<destdir>/<volser>_<N>.sgy bs=128k
             - Lê cada segmento (file mark) da tape como blob bruto
             - Se 0 bytes -> EOF, remover, parar loop
             - Se RC=0 -> .sgy
             - Se RC!=0 mas tem dados -> .err
          5. mtx unload
          6. sleep(10)

        Retorna um TapeManifest com a lista de arquivos extraídos.
        """
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        if volser is None:
            volser = f"SLOT{slot:03d}"

        manifest = TapeManifest(
            volser=volser,
            slot=slot,
            output_dir=str(output_dir),
            status="ok",
        )

        self.log.info("=== EXTRAÇÃO dd (bruto): slot=%d drive=%s volser=%s -> %s ===",
                      slot, nst_device, volser, output_dir)
        print(f"  [drive {nst_device}] Extraindo slot {slot} ({volser}) -> {output_dir}")

        # 1. mtx load slot -> drive
        try:
            self.mtx.load(slot, drive_index)
        except Exception as exc:
            manifest.status = "error"
            manifest.message = f"Falha mtx load: {exc}"
            self.log.error("Falha no load do slot %d: %s", slot, exc)
            return manifest

        try:
            # 2. sleep(5) para estabilizar após load
            time.sleep(5)

            # 3. mt rewind (funciona para leitura)
            self._mt_rewind(nst_device)

            # 3b. mt setblk 0 — coloca a tape em modo de bloco variável
            # Isto é CRÍTICO para ler tapes escritas por tar -cvf.
            # Sem isto, dd retorna "Cannot allocate memory" (ENOMEM) porque
            # o driver st não consegue alocar buffer para o bloco fixo.
            self._mt_setblk_0(nst_device)

            # 4. dd loop: lê cada segmento da tape como arquivo bruto .sgy
            file_num = 1
            while True:
                out_file = output_dir / f"{volser}_{file_num:03d}.sgy"
                rc, size = self._dd_read_file(nst_device, out_file)

                if size == 0:
                    # EOF: arquivo vazio = fim dos dados na tape
                    if out_file.exists():
                        try:
                            out_file.unlink()
                        except OSError:
                            pass
                    self.log.info(
                        "EOF atingido para %s após %d arquivo(s).",
                        volser, file_num - 1,
                    )
                    break

                if rc == 0:
                    # Sucesso: mantém como .sgy
                    manifest.files.append(ExtractedFile(
                        path=out_file.name,
                        size=size,
                        volser=volser,
                        slot=slot,
                        extracted=True,
                        status="ok",
                    ))
                    self.log.info(
                        "Extraído %s (%d bytes, RC=0) via dd",
                        out_file.name, size,
                    )
                else:
                    # Erro mas tem dados: renomeia para .err
                    err_file = output_dir / f"{volser}_{file_num:03d}.err"
                    try:
                        out_file.rename(err_file)
                    except OSError as exc:
                        self.log.warning(
                            "Falha ao renomear %s -> %s: %s",
                            out_file, err_file, exc,
                        )
                    manifest.files.append(ExtractedFile(
                        path=err_file.name,
                        size=size,
                        volser=volser,
                        slot=slot,
                        extracted=True,
                        status="err",
                    ))
                    self.log.warning(
                        "Extraído %s com erro (RC=%d, %d bytes) -> .err",
                        err_file.name, rc, size,
                    )

                file_num += 1

            manifest.status = "ok" if manifest.files else "ok"
            if manifest.files:
                total_size = sum(f.size for f in manifest.files if f.extracted)
                manifest.message = (
                    f"Extraídos {len(manifest.files)} arquivo(s) via dd "
                    f"({total_size} bytes)"
                )
            else:
                manifest.message = "Tape vazia (nenhum arquivo extraído)"

        except ExtractorError as exc:
            manifest.status = "error"
            manifest.message = str(exc)
            self.log.error("Erro na extração do slot %d: %s", slot, exc)
        except Exception as exc:
            manifest.status = "error"
            manifest.message = f"Erro inesperado: {exc}"
            self.log.error("Erro inesperado na extração do slot %d: %s",
                           slot, exc)
        finally:
            # 5. mtx unload (sempre, mesmo em caso de erro)
            self._safe_unload(slot, drive_index)
            # 6. sleep(10)
            time.sleep(10)

        return manifest

    def extract_batch(self, slots: list[int], drive_index: int,
                       nst_device: str, output_dir: str | Path,
                       volser_map: dict[int, str] | None = None) -> ExtractionBatch:
        """Extrai múltiplas tapes (uma por slot) para o mesmo output_dir.

        volser_map: mapeamento slot -> volser (opcional). Se None, usa
        SLOT{slot:03d} como volser.

        Retorna um ExtractionBatch com todos os manifestos.
        """
        batch_id = f"batch_{int(time.time())}"
        output_dir = Path(output_dir).resolve()
        batch = ExtractionBatch(
            batch_id=batch_id,
            output_dir=str(output_dir),
        )
        self.log.info("=== EXTRAÇÃO BATCH dd (bruto): %d tapes -> %s ===",
                      len(slots), output_dir)

        volser_map = volser_map or {}
        for slot in slots:
            volser = volser_map.get(slot, f"SLOT{slot:03d}")
            manifest = self.extract_tape(slot, drive_index, nst_device,
                                          output_dir, volser=volser)
            batch.add_manifest(manifest)
            print(f"  [{manifest.status}] {manifest.volser}: {manifest.message}")

            # Persiste o batch após cada tape (para retomar)
            batch_path = output_dir / f"{batch_id}.json"
            try:
                batch.to_json(batch_path)
            except Exception as exc:
                self.log.warning("Falha ao persistir batch %s: %s",
                                  batch_path, exc)

        # Status final do batch
        ok = sum(1 for m in batch.manifests if m.status == "ok")
        errors = sum(1 for m in batch.manifests if m.status == "error")
        if errors == 0:
            batch.status = "completed"
        elif ok == 0:
            batch.status = "failed"
        else:
            batch.status = "partial"
        batch.finished_at = time.time()

        # Persiste estado final
        batch_path = output_dir / f"{batch_id}.json"
        try:
            batch.to_json(batch_path)
            self.log.info("Batch persistido: %s (status=%s)", batch_path,
                           batch.status)
        except Exception as exc:
            self.log.warning("Falha ao persistir batch final %s: %s",
                              batch_path, exc)

        return batch

    # -----------------------------------------------------------------
    # Internos
    # -----------------------------------------------------------------
    def _mt_rewind(self, nst_device: str) -> None:
        """Executa mt -f <dev> rewind. Funciona para leitura."""
        try:
            proc = subprocess.run(
                [self.cfg.drives.mt_bin, "-f", nst_device, "rewind"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExtractorError(
                f"Timeout no mt rewind para {nst_device}"
            ) from exc
        if proc.returncode != 0:
            self.log.warning(
                "mt rewind retornou RC=%d para %s: %s. Tentando em 5s...",
                proc.returncode, nst_device, (proc.stderr or "").strip(),
            )
            time.sleep(5)
            try:
                proc = subprocess.run(
                    [self.cfg.drives.mt_bin, "-f", nst_device, "rewind"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, timeout=120, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ExtractorError(
                    f"Timeout no mt rewind (retry) para {nst_device}"
                ) from exc
            if proc.returncode != 0:
                raise ExtractorError(
                    f"mt rewind falhou (RC={proc.returncode}) para {nst_device}: "
                    f"{(proc.stderr or '').strip()}"
                )
        self.log.info("Tape rebobinada em %s", nst_device)

    def _mt_setblk_0(self, nst_device: str) -> None:
        """Executa mt -f <dev> setblk 0 — modo de bloco variável.

        CRÍTICO para ler tapes escritas por tar -cvf no mhvtl.
        Sem isto, dd retorna "Cannot allocate memory" (ENOMEM) porque
        o driver st tenta alocar buffer para bloco fixo e falha.

        setblk 0 = modo variável: cada read() retorna um bloco da tape
        (do tamanho em que foi escrito), independente do bs do dd.
        """
        try:
            proc = subprocess.run(
                [self.cfg.drives.mt_bin, "-f", nst_device, "setblk", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.log.warning("Timeout no mt setblk 0 para %s: %s",
                             nst_device, exc)
            return
        if proc.returncode != 0:
            self.log.warning(
                "mt setblk 0 retornou RC=%d para %s: %s "
                "(continuando — pode funcionar mesmo assim)",
                proc.returncode, nst_device, (proc.stderr or "").strip(),
            )
        else:
            self.log.info("Tape em modo bloco variável (setblk 0) em %s",
                          nst_device)

    def _dd_read_file(self, nst_device: str, out_file: Path) -> tuple[int, int]:
        """Executa dd if=<dev> of=<file> bs=128k.

        Retorna (rc, bytes_escritos).

        dd lê um segmento da tape (até o próximo file mark) e escreve
        no arquivo de saída como blob bruto.

        IMPORTANTE:
        - NÃO usa iflag=fullblock (causa ENOMEM no mhvtl)
        - Usa bs=128k (buffer grande o suficiente para qualquer bloco)
        - Requer mt setblk 0 antes (modo bloco variável)
        - Em modo variável, cada read() retorna um bloco da tape
        - dd escreve cada bloco direto no arquivo de saída
        - No file mark, read() retorna 0 = EOF
        """
        # Block size 128k — grande o suficiente para blocos de tar (10k)
        # sem causar ENOMEM no driver st do mhvtl
        bs = "128k"
        cmd = [
            "dd",
            f"if={nst_device}",
            f"of={str(out_file)}",
            f"bs={bs}",
        ]
        self.log.info("Executando: %s", " ".join(cmd))
        print(f"    dd: lendo segmento -> {out_file.name} (bs={bs})")

        # Remove arquivo pré-existente
        if out_file.exists():
            try:
                out_file.unlink()
            except OSError:
                pass

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.cfg.extractor.dd_timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.log.error("Timeout no dd para %s -> %s", nst_device, out_file)
            size = out_file.stat().st_size if out_file.exists() else 0
            return -1, size

        stderr_text = ""
        if proc.stderr:
            stderr_text = (proc.stderr.decode("utf-8", errors="replace")
                           if isinstance(proc.stderr, bytes)
                           else proc.stderr)
        self.log.info("dd RC=%d stderr: %s", proc.returncode,
                       stderr_text.strip()[:300])

        size = out_file.stat().st_size if out_file.exists() else 0
        return proc.returncode, size

    def _safe_unload(self, slot: int, drive_index: int) -> None:
        """Descarrega a tape com tolerância a falhas."""
        try:
            self.mtx.unload(slot, drive_index)
            self.log.info("Tape descarregada do drive %d para slot %d",
                          drive_index, slot)
        except Exception as exc:
            self.log.warning("Falha no unload do slot %d: %s", slot, exc)
            # Tenta achar um slot livre
            try:
                status = self.mtx.status()
                free = status.first_free_slot()
                if free and free != slot:
                    self.log.info("Tentando unload para slot livre %d", free)
                    self.mtx.unload(free, drive_index)
            except Exception as inner:
                self.log.error("Unload persistente falhou: %s", inner)
