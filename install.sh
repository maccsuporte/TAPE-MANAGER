#!/usr/bin/env bash
# =============================================================================
# Tape Manager - Instalador multi-distribuição
# =============================================================================
# Suporte:
#   - Red Hat Enterprise Linux (RHEL) 8, 9 e futuras
#   - CentOS / Rocky Linux / AlmaLinux 8+
#   - OpenSUSE Leap 16+ (e Tumbleweed)
#   - Debian 12+ (bookworm)
#   - Ubuntu 22.04 LTS, 24.04 LTS e futuras
#
# Uso:
#   sudo ./install.sh                # instala tudo
#   sudo ./install.sh --check        # apenas detecta distro e mostra plano
#   sudo ./install.sh --system-deps  # só pacotes do SO
#   sudo ./install.sh --python-deps  # só dependências Python
#   sudo ./install.sh --uninstall    # remove pacotes instalados
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_NAME="Tape Manager"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/.venv}"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=9

# Cores (desativadas se não for TTY)
if [ -t 1 ]; then
    C_RED='\033[0;31m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'
    C_BLUE='\033[0;34m'; C_BOLD='\033[1m'; C_RESET='\033[0m'
else
    C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_BOLD=''; C_RESET=''
fi

log()     { echo -e "${C_BLUE}[$(date +%H:%M:%S)]${C_RESET} $*"; }
ok()      { echo -e "${C_GREEN}[OK]${C_RESET} $*"; }
warn()    { echo -e "${C_YELLOW}[WARN]${C_RESET} $*"; }
err()     { echo -e "${C_RED}[ERR]${C_RESET} $*" >&2; }
die()     { err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Detecção de distribuição (via /etc/os-release)
# ---------------------------------------------------------------------------
detect_distro() {
    if [ ! -r /etc/os-release ]; then
        die "Não foi possível ler /etc/os-release. Distribuição não suportada."
    fi
    # shellcheck disable=SC1091
    . /etc/os-release

    DISTRO_ID="${ID:-unknown}"
    DISTRO_ID_LIKE="${ID_LIKE:-}"
    DISTRO_VERSION="${VERSION_ID:-0}"
    DISTRO_PRETTY="${PRETTY_NAME:-${DISTRO_ID} ${DISTRO_VERSION}}"

    # Família: rhel | suse | debian
    case "${DISTRO_ID}:${DISTRO_ID_LIKE}" in
        rhel:*|centos:*|rocky:*|almalinux:*|fedora:*)
            DISTRO_FAMILY="rhel"
            ;;
        *rhel*)
            DISTRO_FAMILY="rhel"
            ;;
        opensuse*:*|suse:*|sles:*)
            DISTRO_FAMILY="suse"
            ;;
        *suse*|*sles*)
            DISTRO_FAMILY="suse"
            ;;
        debian:*|ubuntu:*|linuxmint:*)
            DISTRO_FAMILY="debian"
            ;;
        *debian*|*ubuntu*)
            DISTRO_FAMILY="debian"
            ;;
        *)
            die "Distribuição não suportada: ${DISTRO_ID} (like=${DISTRO_ID_LIKE}).
Suportadas: RHEL 8+, CentOS/Rocky/Alma 8+, OpenSUSE Leap 16+, Debian 12+, Ubuntu 22.04+."
            ;;
    esac

    # Package manager
    case "${DISTRO_FAMILY}" in
        rhel)
            if command -v dnf >/dev/null 2>&1; then
                PKG_MGR="dnf"
            elif command -v yum >/dev/null 2>&1; then
                PKG_MGR="yum"
            else
                die "Família RHEL sem dnf/yum."
            fi
            ;;
        suse)
            PKG_MGR="zypper"
            ;;
        debian)
            PKG_MGR="apt-get"
            ;;
    esac

    log "Distribuição detectada: ${C_BOLD}${DISTRO_PRETTY}${C_RESET}"
    log "Família: ${DISTRO_FAMILY} | Gerenciador: ${PKG_MGR}"
}

# ---------------------------------------------------------------------------
# Verifica versão mínima do Python
# ---------------------------------------------------------------------------
check_python() {
    local py=""
    # Procura python3.X mais novo primeiro (módulos RHEL)
    for ver in 3.12 3.11 3.10 3.9; do
        if command -v "python${ver}" >/dev/null 2>&1; then
            py="python${ver}"
            break
        fi
    done
    [ -z "${py}" ] && py="python3"

    if ! command -v "${py}" >/dev/null 2>&1; then
        die "Python não encontrado. Instale Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+."
    fi

    local actual_major actual_minor
    actual_major=$("${py}" -c 'import sys; print(sys.version_info[0])')
    actual_minor=$("${py}" -c 'import sys; print(sys.version_info[1])')

    if [ "${actual_major}" -lt "${PYTHON_MIN_MAJOR}" ] || \
       { [ "${actual_major}" -eq "${PYTHON_MIN_MAJOR}" ] && \
         [ "${actual_minor}" -lt "${PYTHON_MIN_MINOR}" ]; }; then
        die "Python ${actual_major}.${actual_minor} é muito antigo. Mínimo: ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+."
    fi

    PYTHON_BIN="${py}"
    ok "Python: ${py} (${actual_major}.${actual_minor})"
}

# ---------------------------------------------------------------------------
# Pacotes do SO por família
# ---------------------------------------------------------------------------
install_system_deps_rhel() {
    local major_version
    major_version=$(echo "${DISTRO_VERSION}" | cut -d. -f1)

    # EPEL é necessário para mtx e mt-st em RHEL/CentOS/Rocky/Alma
    if [ "${major_version}" -ge 9 ]; then
        log "Instalando EPEL (RHEL ${major_version})..."
        ${PKG_MGR} install -y epel-release || warn "epel-release não disponível (já habilitado?)"
    elif [ "${major_version}" -eq 8 ]; then
        log "Instalando EPEL (RHEL 8)..."
        ${PKG_MGR} install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
            || warn "EPEL install falhou (já instalado?)"
    fi

    log "Instalando pacotes do SO (família RHEL)..."
    ${PKG_MGR} install -y \
        mtx \
        mt-st \
        lsscsi \
        sg3_utils \
        tar \
        "${PYTHON_BIN}" \
        "$([ "${major_version}" -ge 9 ] && echo "${PYTHON_BIN}-pip" || true)" \
        2>&1 || {
            # RHEL 8 pode precisar de python3-pip via dnf module
            warn "Tentando instalar pip via ensurepip..."
            ${PKG_MGR} install -y "${PYTHON_BIN}-pip" || "${PYTHON_BIN}" -m ensurepip --upgrade || true
        }

    # Em RHEL 8/9 pode ser necessário habilitar módulo python mais novo
    if [ "${major_version}" -eq 8 ] && [ "${PYTHON_BIN}" = "python3.11" ]; then
        warn "RHEL 8: certifique-se de habilitar o módulo python3.11 via:
  sudo dnf module enable python3.11"
    fi
}

install_system_deps_suse() {
    log "Instalando pacotes do SO (família OpenSUSE)..."
    ${PKG_MGR} --non-interactive install \
        mtx \
        mt-st \
        lsscsi \
        sg3_utils \
        tar \
        python3 \
        python3-pip \
        python3-PyYAML \
        2>&1
}

install_system_deps_debian() {
    log "Atualizando índice de pacotes (família Debian/Ubuntu)..."
    ${PKG_MGR} update -y

    log "Instalando pacotes do SO..."
    ${PKG_MGR} install -y \
        mtx \
        mt-st \
        lsscsi \
        sg3-utils \
        tar \
        python3 \
        python3-pip \
        python3-venv \
        python3-yaml \
        2>&1
}

install_system_deps() {
    case "${DISTRO_FAMILY}" in
        rhel)   install_system_deps_rhel ;;
        suse)   install_system_deps_suse ;;
        debian) install_system_deps_debian ;;
    esac
    ok "Pacotes do SO instalados."
}

# ---------------------------------------------------------------------------
# Dependências Python (virtualenv + pip)
# ---------------------------------------------------------------------------
install_python_deps() {
    log "Criando virtualenv em ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
    # shellcheck disable=SC1091
    . "${VENV_DIR}/bin/activate"
    pip install --upgrade pip
    pip install -r "${PROJECT_DIR}/requirements.txt"
    ok "Dependências Python instaladas no virtualenv."
    log "Para ativar: source ${VENV_DIR}/bin/activate"
}

# ---------------------------------------------------------------------------
# Desinstalação
# ---------------------------------------------------------------------------
uninstall() {
    warn "Isto removerá o virtualenv ${VENV_DIR} e os pacotes do SO."
    read -r -p "Continuar? [y/N] " ans
    [ "${ans}" != "y" ] && { log "Cancelado."; exit 0; }

    if [ -d "${VENV_DIR}" ]; then
        rm -rf "${VENV_DIR}"
        ok "Virtualenv removido."
    fi

    case "${DISTRO_FAMILY}" in
        rhel)
            ${PKG_MGR} remove -y mtx mt-st lsscsi sg3_utils 2>&1 || true
            ;;
        suse)
            ${PKG_MGR} --non-interactive remove mtx mt-st lsscsi sg3_utils 2>&1 || true
            ;;
        debian)
            ${PKG_MGR} remove -y mtx mt-st lsscsi sg3-utils 2>&1 || true
            ;;
    esac
    ok "Pacotes do SO removidos."
}

# ---------------------------------------------------------------------------
# Modo check (apenas mostra o plano, não instala nada)
# ---------------------------------------------------------------------------
show_plan() {
    echo
    echo -e "${C_BOLD}=== PLANO DE INSTALAÇÃO ===${C_RESET}"
    echo "Distribuição: ${DISTRO_PRETTY}"
    echo "Família:      ${DISTRO_FAMILY}"
    echo "Gerenciador:  ${PKG_MGR}"
    echo "Python:       ${PYTHON_BIN:-não verificado}"
    echo
    echo -e "${C_BOLD}Pacotes do SO:${C_RESET}"
    case "${DISTRO_FAMILY}" in
        rhel)
            echo "  - epel-release"
            echo "  - mtx mt-st lsscsi sg3_utils tar ${PYTHON_BIN} ${PYTHON_BIN}-pip"
            ;;
        suse)
            echo "  - mtx mt-st lsscsi sg3_utils tar python3 python3-pip python3-PyYAML"
            ;;
        debian)
            echo "  - mtx mt-st lsscsi sg3-utils tar python3 python3-pip python3-venv python3-yaml"
            ;;
    esac
    echo
    echo -e "${C_BOLD}Python (via pip no virtualenv):${C_RESET}"
    echo "  - PyYAML>=6.0"
    echo
    echo "Para executar a instalação: sudo $0"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    local mode="${1:-install}"

    [ "$(id -u)" -ne 0 ] && [ "${mode}" != "--check" ] && \
        warn "Executando sem root: install system deps pode falhar. Use sudo."

    detect_distro

    case "${mode}" in
        --check)
            check_python
            show_plan
            ;;
        --system-deps)
            check_python
            install_system_deps
            ;;
        --python-deps)
            check_python
            install_python_deps
            ;;
        --uninstall)
            uninstall
            ;;
        install|"")
            check_python
            install_system_deps
            install_python_deps
            ok "${PROJECT_NAME} instalado com sucesso!"
            echo
            log "Próximos passos:"
            echo "  1. source ${VENV_DIR}/bin/activate"
            echo "  2. Configure config/config.yaml (drives.dedicated_serial)"
            echo "  3. python -m tape_manager --command list_drives"
            echo "  4. python -m tape_manager --command status"
            echo "  5. python -m tape_manager  (menu interativo)"
            ;;
        *)
            die "Uso: $0 [--check|--system-deps|--python-deps|--uninstall]"
            ;;
    esac
}

main "$@"
