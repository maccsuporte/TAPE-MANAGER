# Tape Manager

Sistema modular de gestão de tape library com suporte a multi-drive paralelo,
multi-cartucho, e extração via dd.

## Requisitos

- Python 3.9+
- `mtx` (controle do changer)
- `mt` (controle do drive de fita)
- `tar` (gravação)
- `dd` (extração)
- `lsscsi` (descoberta de devices)
- `sg_inq` (opcional, identificação por serial)
- PyYAML (`pip install pyyaml`)

## Instalação

```bash
# 1. Extrair o projeto
unzip tape_manager.zip -d /root/root/

# 2. Executar o instalador
bach install.sh ou ./install.sh

# 3. Criar ambiente virtual
cd /root/root/tape_manager
python3 -m venv .venv
source .venv/bin/activate

# 4. Verificar instalação
python -m tape_manager --version
```

## Modos de Operação

O Tape Manager opera em dois modos:

### Modo Físico (padrão, sem flag)

```bash
python -m tape_manager
```

- Opera apenas com **drives físicos reais**
- Correlação de drives via **SCSI host** (sysfs)
- Cartuchos virtuais **JRT01 e dummy são ocultos**
- Disponíveis: JAE05, JAE06, JBE05, JBE06, JBE07, JCE07

### Modo mhvtl (Virtual Tape Library)

```bash
python -m tape_manager --mhvtl
# ou atalho:
python -m tape_manager -m
# ou (também aceito):
python -m tape_manager -mhvtl
```

- Habilita correlação **mhvtl** (device.conf + WWN/serial)
- Cartuchos virtuais **JRT01 e dummy disponíveis** (para testes)
- Disponíveis: JAE05, JAE06, JBE05, JBE06, JBE07, JCE07, JRT01, dummy

## Comandos

### Menu Interativo

```bash
# Modo físico
python -m tape_manager

# Modo mhvtl
python -m tape_manager --mhvtl
```

Menu com 11 opções:
1. Status da Tape Library
2. Inventário
3. Listar drives disponíveis
4. Criar plano de gravação
5. Validar plano
6. Executar plano (multi-drive paralelo)
7. Verificar progresso
8. Retomar execução
9. Extrair tapes para disco (multi-drive paralelo, dd)
10. Apagar cartucho (mt erase)
11. Status do drive

### Comandos Diretos (--command)

| Comando | Descrição | Argumentos requeridos |
|---------|-----------|----------------------|
| `status` | Exibe status da tape library | — |
| `inventory` | Executa inventário da biblioteca | — |
| `list_drives` | Lista drives /dev/nst* disponíveis | — |
| `drive_status` | Status detalhado de um drive | — |
| `create_plan` | Cria plano de gravação | `--survey-dir`, `--cartridge`, `--format` |
| `run_plan` | Executa plano de gravação | `--plan-file` |
| `extract` | Extrai tapes para disco via dd | `--slots`, `--output-dir` |
| `erase` | Apaga cartucho via mt erase | `--slots` |

## Exemplos

### Status da library

```bash
# Modo físico
python -m tape_manager --command status

# Modo mhvtl
python -m tape_manager --mhvtl --command status
```

### Listar drives disponíveis

```bash
python -m tape_manager --mhvtl --command list_drives
```

### Criar plano de gravação

```bash
python -m tape_manager --mhvtl \
    --command create_plan \
    --survey-dir /survay \
    --cartridge auto \
    --format tar
```

O plano é salvo em `output/<survey_name>/plan.json`.

### Executar plano

```bash
python -m tape_manager --mhvtl \
    --command run_plan \
    --plan-file /root/root/tape_manager/output/survay/plan.json
```

### Extrair tapes para disco

```bash
python -m tape_manager --mhvtl \
    --command extract \
    --slots 1,2,3 \
    --output-dir /restore
```

Cada tape é extraída como arquivo bruto `.sgy` via `dd` (com `mt setblk 0`
para modo de bloco variável).

### Apagar cartucho

```bash
python -m tape_manager --mhvtl \
    --command erase \
    --slots 5
```

## Arquitetura

```
tape_manager/
├── config/config.yaml              # Configuração centralizada
├── requirements.txt
└── tape_manager/
    ├── __init__.py
    ├── __main__.py                 # Entry point: CLI + menu interativo
    ├── config.py                   # Dataclass Config + parser YAML
    ├── logger.py                   # Logging configurável
    ├── exceptions.py               # Exceções de domínio
    ├── models.py                   # FileEntry, VolumePlan, SurveyPlan, etc.
    ├── drive_resolver.py           # Resolução de drive por serial SCSI
    ├── mtx_controller.py           # Controlador mtx (thread-safe + cooldown)
    ├── mhvtl.py                    # Parser device.conf + correlação WWN
    ├── planner.py                  # Planner FFD multi-cartucho
    ├── tape_writer.py              # Gravação via tar -cvf
    ├── tape_extractor.py           # Extração via dd + mt setblk 0
    ├── integrity.py                # Pré/pós-validação + ProgressTracker
    ├── operator_alerts.py          # Espera de tape do operador
    └── executor.py                 # Orquestrador multi-drive paralelo
```

## Características Principais

### Gravação (tar -cvf)
- NUNCA usa `mt` antes de `tar -cvf` (evita "Device busy" no mhvtl)
- `tar -cvf` auto-rebobina ao abrir `/dev/nstN`
- Retry 3x/10s para "Device busy"
- Pós-validação NÃO FATAL (tape está no fim após gravação)

### Extração (dd + mt setblk 0)
- `mt setblk 0` antes de ler (modo bloco variável — CRÍTICO)
- `dd if=/dev/nstN of=<file>.sgy bs=128k` (sem iflag=fullblock)
- Cada segmento (file mark) vira um arquivo `.sgy` bruto
- Sem interpretação de conteúdo (blob raw)

### Multi-Drive Paralelo
- Threads independentes por `/dev/nst*`
- `_mtx_lock` serializa operações mtx (1 picker físico)
- Cooldown de 1s entre MOVE MEDIUM (estabilização do picker)
- Retry 3x/5s para Hardware Error no mtx load

### Detecção de Drive Quebrado
- Hardware Error persistente → drive marcado como QUEBRADO
- Volumes restantes redistribuídos para drive funcional (fase 2)
- "illegal drive-number" também detectado (DTE inexistente)

### Correlação de Drives
1. SCSI host (sysfs) — para hardware real
2. mhvtl (device.conf + WWN/serial) — apenas com `--mhvtl`
3. Heurística single-drive (1 DTE + 1 device base)

### Filtro de Drives Inacessíveis
- Drives com `DTE=None` são ocultos do menu
- Ex: library 30 do mhvtl quando só há 1 changer controlando library 10

## Configuração

Editar `config/config.yaml`:

```yaml
# Changer (auto-detecção se null)
changer:
  device: null              # ou "/dev/sg9"

# Drives
drives:
  dedicated_serial: null    # fallback para single-drive

# Cartuchos (capacidades em GiB)
cartridges:
  JBE07:
    tar: 1485
    ltfs: 1350

# mhvtl
mhvtl:
  enabled: true
  auto_correlate: true
```

## Troubleshooting

### "Cannot allocate memory" no dd

Causa: tape em modo bloco fixo, dd não consegue alocar buffer.
Solução: `mt setblk 0` é executado automaticamente antes do dd.

### "Device busy" no tar -cvf

Causa: `mt` executado antes de `tar -cvf` no mhvtl.
Solução: o sistema NUNCA executa `mt` antes de `tar -cvf`. Se persistir,
verifique se não há outro processo usando o `/dev/nst*`.

### "illegal drive-number argument"

Causa: DTE não existe neste changer (ex: DTE 4 em changer com 4 DTEs).
Solução: use apenas drives com DTE válido (0-3). O sistema consolida
automaticamente em drives válidos.

### "Hardware Error" no mtx load

Causa: drive mhvtl com problema ou picker busy.
Solução: o sistema faz 3 retries com 5s de cooldown. Se persistir,
reinicie o mhvtl: `systemctl restart mhvtl`.

### Drive não aparece no menu

Causa: DTE=None (drive inacessível pelo changer).
Solução: verifique se o drive pertence à library controlada pelo changer.
Use `--mhvtl` se estiver usando Virtual Tape Library.
