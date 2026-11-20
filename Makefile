.PHONY: help install install-dev install-audio install-models check-env demo quick test test-cpu test-gpu api api-reload gpu-check gpu-smoke spdx-check docker-build docker-run models models-verify cleanup-jobs clean

PYTHON ?= python
PORT ?= 8080

help:  ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install:  ## install the runtime dependencies
	$(PYTHON) -m pip install -e .

install-dev:  ## install the runtime and development dependencies
	$(PYTHON) -m pip install -e ".[dev]"

install-audio:  ## add the DeepFilterNet audio runtime
	$(PYTHON) -m pip install -e ".[audio]"

install-models:  ## add the ONNX Runtime used by RIFE
	$(PYTHON) -m pip install -e ".[models]"

check-env:  ## report the environment and validate the configuration
	$(PYTHON) scripts/check_environment.py

demo:  ## end-to-end demo on generated footage
	$(PYTHON) demo.py

quick:  ## end-to-end demo without the interpolation stage
	$(PYTHON) demo.py --quick

test:  ## run the whole test suite (GPU tests are skipped without CUDA)
	$(PYTHON) -m pytest -q

test-cpu:  ## run the CPU suite explicitly
	$(PYTHON) -m pytest -m "not gpu" -v

test-gpu:  ## run the GPU-marked tests (needs a CUDA host)
	$(PYTHON) -m pytest -m gpu -v

api:  ## serve the API
	uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

api-reload:  ## serve the API with auto-reload
	uvicorn app.main:app --host 0.0.0.0 --port $(PORT) --reload

gpu-check:  ## report the GPU capabilities of this machine
	$(PYTHON) -m app.gpu_check --all

gpu-smoke:  ## the GPU validation tool (exits non-zero without a GPU)
	$(PYTHON) scripts/gpu_smoke_test.py

spdx-check:  ## verify the SPDX headers and the licence
	$(PYTHON) scripts/check_spdx.py

models:  ## fetch the model weights
	$(PYTHON) scripts/download_models.py

models-verify:  ## verify the model weights against their pinned digests
	$(PYTHON) scripts/download_models.py --verify-only

docker-build:  ## build the CPU image
	docker build -t framefrog-pipeline:cpu .

docker-run:  ## run the CPU image
	docker run --rm -p $(PORT):$(PORT) -e APP_ENV=development framefrog-pipeline:cpu

cleanup-jobs:  ## remove finished jobs past the retention window
	$(PYTHON) scripts/cleanup_jobs.py

clean:  ## remove generated artefacts
	rm -rf outputs work assets/sample .pytest_cache .mypy_cache .ruff_cache
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
