#!/usr/bin/env bash
set -Eeuo pipefail

config=/mnt/f/AI-GAME/config/model-runtime.env
if [[ ! -f "$config" ]]; then
  echo "Missing runtime config: $config" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$config"

runtime="$AI_GAME_RUNTIME_ROOT"
venv="$runtime/envs/gui-owl"
uv_cache="$runtime/cache/uv"
hf_cache="$runtime/cache/huggingface"
mkdir -p "$runtime/envs" "$uv_cache" "$hf_cache" "$runtime/logs" "$runtime/run"

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x /root/.local/bin/uv ]]; then
  uv_bin=/root/.local/bin/uv
fi
if [[ -z "$uv_bin" ]]; then
  echo 'uv is required inside WSL but was not found.' >&2
  exit 1
fi

export UV_CACHE_DIR="$uv_cache"
export HF_HOME="$hf_cache"
export HF_XET_HIGH_PERFORMANCE=1

if [[ ! -x "$venv/bin/python" ]]; then
  "$uv_bin" venv --python 3.12 --seed "$venv"
fi

"$uv_bin" pip install --python "$venv/bin/python" --torch-backend=auto \
  'vllm==0.26.0' \
  'qwen-vl-utils==0.0.14'
"$uv_bin" pip check --python "$venv/bin/python"

{
  date --iso-8601=seconds
  "$venv/bin/python" --version
  "$venv/bin/python" -c 'import torch; print("torch=" + torch.__version__); print("cuda=" + str(torch.version.cuda)); print("cuda_available=" + str(torch.cuda.is_available()))'
  "$venv/bin/python" -c 'import vllm; print("vllm=" + vllm.__version__)'
  "$venv/bin/python" -c 'import transformers; print("transformers=" + transformers.__version__)'
  nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free,compute_cap --format=csv,noheader
} > "$runtime/run/environment-versions.txt"

echo "GUI model environment is ready at $venv"
echo 'Pinned model download is handled by the Windows bootstrap wrapper.'
cat "$runtime/run/environment-versions.txt"
