"""Leitura e escrita de labels de tape.

CORREÇÕES ACUMULADAS (críticas):

1. **Leitura de label**: usar `mt rewind` + `dd bs=10240 count=1` +
   Python `tarfile` para parsear. NÃO usar `tar -xOf` (falha com
   I/O error quando a tape tem dados após o label).

2. **mt rewind FUNCIONA para leitura**: o busy só acontece quando mt
   precede tar -cvf (escrita). Para leitura (dd/tar -xOf), o mt rewind
   é seguro e necessário.

Fluxo de leitura de label:
  1. mt -f /dev/nstN rewind   (FUNCIONA para leitura)
  2. sleep(1)
  3. dd if=/dev/nstN bs=10240 count=1 of=-  (lê 1 record tar = 10240 bytes)
  4. Python tarfile.open(mode="r|", fileobj=BytesIO(data))
  5. Extrai o membro "_TAPE_LABEL.txt" (ou similar)
  6. TapeLabel.deserialize(conteudo_texto)
"""

from __future__ import annotations

import io
import subprocess
import tarfile
import time
from pathlib import Path

from .config import Config
from .exceptions import LabelError
from .logger import get_logger
from .models import TapeLabel


class TapeLabelReader:
    """Lê o label de uma tape carregada no drive.

    Usa mt rewind + dd bs=10240 count=1 + Python tarfile (NÃO usa tar -xOf).
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = get_logger()

    def read_label(self, nst_device: str) -> TapeLabel | None:
        """Lê o label da tape carregada em `nst_device`.

        Retorna TapeLabel ou None se a tape não tiver label.
        Levanta LabelError em caso de falha de hardware.
        """
        self.log.info("Lendo label de %s ...", nst_device)

        # 1. mt rewind (FUNCIONA para leitura)
        self._mt_rewind(nst_device)
        # 2. sleep(1) para o drive estabilizar
        time.sleep(1)

        # 3. dd bs=10240 count=1 (lê 1 record tar)
        data = self._dd_read_first_record(nst_device)
        if not data:
            self.log.warning("dd retornou 0 bytes. Tape pode estar vazia.")
            return None

        # 4. Parse com Python tarfile
        label = self._parse_label_from_tar(data)
        if label is None:
            self.log.warning(
                "Label não encontrado no primeiro record da tape em %s. "
                "Tape pode não ter label (não gravada pelo tape_manager).",
                nst_device,
            )
            return None

        self.log.info(
            "Label lido: volser=%s cartridge=%s volume_index=%d/%d",
            label.volser, label.cartridge, label.volume_index,
            label.total_volumes,
        )
        return label

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
            raise LabelError(
                f"Timeout no mt rewind para {nst_device}"
            ) from exc
        if proc.returncode != 0:
            # Retry após 5s
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
                raise LabelError(
                    f"Timeout no mt rewind (retry) para {nst_device}"
                ) from exc
            if proc.returncode != 0:
                raise LabelError(
                    f"mt rewind falhou (RC={proc.returncode}) para {nst_device}: "
                    f"{(proc.stderr or '').strip()}"
                )
        self.log.info("Tape rebobinada em %s", nst_device)

    def _dd_read_first_record(self, nst_device: str) -> bytes:
        """Executa dd if=<dev> bs=10240 count=1 e retorna os bytes lidos.

        Usa bs=10240 porque o tar escreve em blocos de 10240 bytes
        (20 * 512, default do GNU tar). count=1 pega exatamente 1 bloco,
        que contém o header + início do primeiro arquivo (o label).
        """
        bs = self.cfg.extractor.label_dd_block_size or "10240"
        count = self.cfg.extractor.label_dd_count or "1"
        cmd = [
            "dd", f"if={nst_device}",
            f"bs={bs}", f"count={count}",
            "iflag=fullblock",
        ]
        self.log.debug("Executando: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LabelError(
                f"Timeout no dd para {nst_device}"
            ) from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace") \
                if isinstance(proc.stderr, bytes) else (proc.stderr or "")
            # dd às vezes retorna != 0 mesmo lendo algo; se temos dados, usa
            if not proc.stdout:
                raise LabelError(
                    f"dd falhou (RC={proc.returncode}) para {nst_device}: "
                    f"{stderr.strip()[:300]}"
                )
            self.log.warning(
                "dd retornou RC=%d mas tem dados (%d bytes). Prosseguindo. stderr: %s",
                proc.returncode, len(proc.stdout), stderr.strip()[:200],
            )
        return proc.stdout or b""

    def _parse_label_from_tar(self, data: bytes) -> TapeLabel | None:
        """Faz o parse do primeiro record tar e extrai o label.

        O primeiro record do tar contém o header do primeiro arquivo
        (que deve ser o _TAPE_LABEL.txt) + parte do conteúdo. Como o
        label é pequeno (texto), cabe inteiro no primeiro bloco de 10240
        bytes.

        Estratégia:
          1. Cria um BytesIO com os dados
          2. tarfile.open(mode="r|") lê o stream tar
          3. Para cada membro, extrai o conteúdo
          4. Se o nome do membro contém o label_filename, faz deserialize
        """
        label_filename = self.cfg.planner.label_filename or "_TAPE_LABEL.txt"
        # Normaliza: o label pode estar em subdiretório (survey_name/_TAPE_LABEL.txt)
        label_basename = Path(label_filename).name

        try:
            bio = io.BytesIO(data)
            # mode="r|" lê um stream não-seekable (adequado para dados parciais)
            # Mas como temos os bytes inteiros, podemos usar "r:" (default).
            # Usamos "r|" para tolerar truncagem no fim do record.
            tf = tarfile.open(fileobj=bio, mode="r|", errors="replace")
        except tarfile.TarError as exc:
            self.log.warning(
                "Não foi possível abrir como tar: %s. "
                "Tape pode estar vazia ou não ser um archive tar.", exc,
            )
            return None

        try:
            for member in tf:
                name = member.name
                # Procura pelo arquivo de label (pode estar em subdiretório)
                if Path(name).name == label_basename:
                    try:
                        fobj = tf.extractfile(member)
                        if fobj is None:
                            continue
                        content = fobj.read().decode("utf-8", errors="replace")
                        return TapeLabel.deserialize(content)
                    except Exception as exc:
                        self.log.warning(
                            "Falha ao extrair label do membro %s: %s", name, exc,
                        )
                        continue
                # Se não for o label, continua (não lemos outros arquivos aqui)
            # Se chegou aqui, não encontrou o label no primeiro record
            return None
        finally:
            tf.close()


# ---------------------------------------------------------------------------
# Helper standalone: ler label de um device (sem instanciar classe)
# ---------------------------------------------------------------------------
def read_label_from_device(cfg: Config, nst_device: str) -> TapeLabel | None:
    """Lê o label de uma tape em `nst_device`."""
    reader = TapeLabelReader(cfg)
    return reader.read_label(nst_device)
