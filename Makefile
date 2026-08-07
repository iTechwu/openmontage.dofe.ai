PYTHON_VERSION ?= 3.10
VENV_DIR ?= .venv
BASE_PYTHON ?= $(shell command -v python$(PYTHON_VERSION) 2>/dev/null || command -v python3 2>/dev/null || command -v python 2>/dev/null)
IMAGE_REVISION ?= $(shell git rev-parse --verify HEAD 2>/dev/null || printf unknown)
RUN_PYTHON = $(shell for dir in "$$VIRTUAL_ENV" "$$CONDA_PREFIX" "$(VENV_DIR)"; do if [ -n "$$dir" ] && [ -x "$$dir/bin/python" ]; then printf "%s/bin/python" "$$dir"; exit 0; elif [ -n "$$dir" ] && [ -x "$$dir/Scripts/python.exe" ]; then printf "%s/Scripts/python.exe" "$$dir"; exit 0; fi; done; if [ "$(OS)" = "Windows_NT" ]; then printf "%s/Scripts/python.exe" "$(VENV_DIR)"; else printf "%s/bin/python" "$(VENV_DIR)"; fi)
PIP = $(RUN_PYTHON) -m pip

.DEFAULT_GOAL := setup

.PHONY: setup install install-dev install-gpu install-runtimes runtimes-doctor test test-contracts lint clean preflight demo demo-list hyperframes-doctor hyperframes-warm venv ensure-venv docker-build docker-up docker-down

# ---- Virtual environment ----

ensure-venv:
	@if [ -n "$$VIRTUAL_ENV" ] && { [ -x "$$VIRTUAL_ENV/bin/python" ] || [ -x "$$VIRTUAL_ENV/Scripts/python.exe" ]; }; then \
		echo "==> Using active virtual environment: $$VIRTUAL_ENV"; \
	elif [ -n "$$CONDA_PREFIX" ] && { [ -x "$$CONDA_PREFIX/bin/python" ] || [ -x "$$CONDA_PREFIX/Scripts/python.exe" ]; }; then \
		echo "==> Using active conda environment: $$CONDA_PREFIX"; \
	elif [ -x "$(VENV_DIR)/bin/python" ] || [ -x "$(VENV_DIR)/Scripts/python.exe" ]; then \
		echo "==> Using existing virtual environment: $(VENV_DIR)"; \
	elif command -v uv >/dev/null 2>&1; then \
		echo "==> Creating virtual environment with uv (Python $(PYTHON_VERSION)+): $(VENV_DIR)"; \
		uv venv --python $(PYTHON_VERSION) "$(VENV_DIR)"; \
	else \
		if [ -z "$(BASE_PYTHON)" ]; then \
			echo "ERROR: Python $(PYTHON_VERSION)+ is required, but no python executable was found."; \
			exit 1; \
		fi; \
		"$(BASE_PYTHON)" -c "import sys; required=tuple(map(int, '$(PYTHON_VERSION)'.split('.')[:2])); raise SystemExit(0 if sys.version_info[:2] >= required else 1)" || { \
			echo "ERROR: OpenMontage requires Python $(PYTHON_VERSION)+."; \
			echo "Install uv or Python $(PYTHON_VERSION)+, then run make again."; \
			exit 1; \
		}; \
		echo "==> Creating virtual environment with Python venv: $(VENV_DIR)"; \
		"$(BASE_PYTHON)" -m venv "$(VENV_DIR)" || { \
			echo "ERROR: Could not create $(VENV_DIR). Install uv or ensure python venv support is available."; \
			exit 1; \
		}; \
	fi
	@$(RUN_PYTHON) -c "import sys; required=tuple(map(int, '$(PYTHON_VERSION)'.split('.')[:2])); raise SystemExit(0 if sys.version_info[:2] >= required else 1)" || { \
		echo "ERROR: OpenMontage requires Python $(PYTHON_VERSION)+."; \
		echo "Current interpreter is $$($(RUN_PYTHON) -c 'import sys; print(\".\".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unavailable): $(RUN_PYTHON)"; \
		echo "Activate a compatible environment or remove it so make can create $(VENV_DIR)."; \
		exit 1; \
	}
	@$(RUN_PYTHON) -m pip --version >/dev/null 2>&1 || $(RUN_PYTHON) -m ensurepip --upgrade >/dev/null

venv: ensure-venv
	@echo "==> Virtual environment ready."
	@echo "    Python: $(RUN_PYTHON)"
	@if [ -z "$$VIRTUAL_ENV" ] && [ -z "$$CONDA_PREFIX" ]; then if [ "$(OS)" = "Windows_NT" ]; then echo "    Activate with: $(VENV_DIR)\\Scripts\\Activate.ps1"; else echo "    Activate with: source $(VENV_DIR)/bin/activate"; fi; fi

# ---- One-command setup ----

setup: ensure-venv
	@echo "==> Installing Python dependencies..."
	$(PIP) install -r requirements.txt
	@echo ""
	@$(MAKE) --no-print-directory install-runtimes
	@echo ""
	@echo "==> Installing free offline TTS (Piper)..."
	$(PIP) install piper-tts || echo "  [skip] piper-tts install failed — TTS will use cloud providers instead"
	@echo ""
	$(RUN_PYTHON) -c "import shutil, os; e=os.path.exists('.env'); shutil.copy('.env.example','.env') if not e else None; print('==> Created .env from .env.example — add your API keys there.' if not e else '==> .env already exists — skipping.')"
	@echo ""
	@echo "Done! Open this project in your AI coding assistant and start creating."
	@echo "  Optional: add API keys to .env to unlock cloud providers."
	@echo "  Optional: run 'make install-gpu' if you have an NVIDIA GPU."
	@echo "  Verify both local composition CLIs with: make runtimes-doctor"

# ---- Individual installs ----

install: ensure-venv
	$(PIP) install -r requirements.txt

install-dev: ensure-venv
	$(PIP) install -r requirements-dev.txt

install-gpu: ensure-venv
	$(PIP) install -r requirements-gpu.txt
	$(PIP) install diffusers transformers accelerate

install-runtimes:
	@command -v node >/dev/null 2>&1 || { echo "ERROR: Node.js >= 22 is required for HyperFrames."; exit 1; }
	@node -e "const major=Number(process.versions.node.split('.')[0]); process.exit(major >= 22 ? 0 : 1)" || { echo "ERROR: HyperFrames requires Node.js >= 22 (found $$(node --version))."; exit 1; }
	@command -v ffmpeg >/dev/null 2>&1 || { echo "ERROR: FFmpeg is required by both composition runtimes."; exit 1; }
	@echo "==> Installing pinned Remotion and HyperFrames runtimes..."
	cd remotion-composer && npm ci --no-audit --no-fund
	@echo "==> Preparing local browser runtimes..."
	cd remotion-composer && npm run runtime:browser
	cd remotion-composer && npm run runtime:hyperframes-browser
	cd remotion-composer && npm run runtime:hyperframes >/dev/null

# ---- Testing ----

test: ensure-venv
	$(RUN_PYTHON) -m pytest tests/ -v

test-contracts: ensure-venv
	$(RUN_PYTHON) -m pytest tests/contracts/ -v

# ---- Utilities ----

preflight: ensure-venv
	$(RUN_PYTHON) -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_menu(), indent=2))"

hyperframes-doctor: ensure-venv
	@echo "==> Probing project-local HyperFrames runtime..."
	$(RUN_PYTHON) -c "from tools.video.hyperframes_compose import HyperFramesCompose; import json, sys; r=HyperFramesCompose().execute({'operation':'doctor'}); print(json.dumps(r.data, indent=2)); print('OK' if r.success else f'FAIL: {r.error}'); sys.exit(0 if r.success else 1)"

runtimes-doctor: ensure-venv
	@echo "==> Verifying Remotion package versions..."
	cd remotion-composer && npm run runtime:remotion
	@$(MAKE) --no-print-directory hyperframes-doctor
	@$(RUN_PYTHON) -c "from tools.video.video_compose import VideoCompose; e=VideoCompose().get_info()['render_engines']; missing=[name for name in ('remotion','hyperframes') if not e.get(name)]; print(f'==> OpenMontage composition runtimes: {e}'); raise SystemExit(f'Unavailable runtimes: {missing}' if missing else 0)"

# Backwards-compatible alias. Runtime installation is now lockfile-based.
hyperframes-warm: install-runtimes
	@echo "==> HyperFrames project-local runtime is installed."

demo: ensure-venv
	@echo "==> Rendering zero-key demo videos (no API keys needed)..."
	@echo "    These use only Remotion components — animated charts, text, data viz."
	@echo ""
	$(RUN_PYTHON) render_demo.py

demo-list: ensure-venv
	$(RUN_PYTHON) render_demo.py --list

lint: ensure-venv
	$(RUN_PYTHON) -m py_compile tools/base_tool.py
	$(RUN_PYTHON) -m py_compile tools/tool_registry.py
	$(RUN_PYTHON) -m py_compile tools/cost_tracker.py
	$(RUN_PYTHON) -m py_compile tools/analysis/composition_validator.py

clean:
	$(BASE_PYTHON) -c "import pathlib, shutil; excluded=[pathlib.Path('$(VENV_DIR)'), pathlib.Path('venv')]; skip=lambda p: any(p == root or root in p.parents for root in excluded); roots=[p for p in pathlib.Path('.').rglob('__pycache__') if not skip(p)]; [shutil.rmtree(p) for p in roots]; files=[p for p in pathlib.Path('.').rglob('*.pyc') if not skip(p)]; [p.unlink() for p in files]"

docker-build:
	OPENMONTAGE_IMAGE_REVISION="$(IMAGE_REVISION)" docker compose build openmontage-mcp

docker-up:
	docker compose up -d openmontage-mcp

docker-down:
	docker compose down
