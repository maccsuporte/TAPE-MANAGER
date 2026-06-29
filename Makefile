# =============================================================================
# Tape Manager - Makefile
# =============================================================================
# Atalhos para operações comuns em qualquer distribuição suportada.
# Uso: make <alvo>
# =============================================================================

PYTHON ?= python3
VENV   ?= .venv
CONFIG ?= config/config.yaml

.PHONY: help install check system-deps python-deps venv test clean \
        status inventory list-drives menu lint

help: ## Mostra esta ajuda
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Instala tudo (detecção automática de distro)
	sudo ./install.sh

check: ## Apenas detecta distro e mostra plano (não instala)
	./install.sh --check

system-deps: ## Instala apenas pacotes do SO
	sudo ./install.sh --system-deps

python-deps: venv ## Instala apenas dependências Python no virtualenv
	@echo "Virtualenv em $(VENV)"

venv: ## Cria virtualenv e instala dependências Python
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt
	@echo "Ative com: source $(VENV)/bin/activate"

test: ## Roda testes rápidos de sanidade
	@echo ">>> Sintaxe Python"
	@find tape_manager -name '*.py' -exec $(PYTHON) -m py_compile {} +
	@echo ">>> Config YAML"
	@$(PYTHON) -c "from tape_manager.config import Config, DEFAULT_CONFIG_PATH; Config.from_yaml(DEFAULT_CONFIG_PATH).validate(); print('OK')"
	@echo ">>> CLI"
	@$(PYTHON) -m tape_manager --version

status: ##mtx status da library
	$(PYTHON) -m tape_manager --config $(CONFIG) --command status

inventory: ## Inventário da library
	$(PYTHON) -m tape_manager --config $(CONFIG) --command inventory

list-drives: ## Lista drives SCSI e resolve por serial
	$(PYTHON) -m tape_manager --config $(CONFIG) --command list_drives

menu: ## Abre menu interativo
	$(PYTHON) -m tape_manager --config $(CONFIG)

clean: ## Remove caches e virtualenv
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -rf $(VENV) .pytest_cache .mypy_cache

lint: ## Lint rápido
	$(PYTHON) -m pyflakes tape_manager/ 2>/dev/null || \
		@echo "pyflakes não instalado (pip install pyflakes para habilitar)"
