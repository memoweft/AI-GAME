#!/usr/bin/env bash
set -Eeuo pipefail

config=/mnt/f/AI-GAME/config/model-runtime.env
# shellcheck disable=SC1090
source "$config"

runtime="$AI_GAME_RUNTIME_ROOT"
venv="$runtime/envs/gui-owl"
pid_file="$runtime/run/gui-model.pid"
model_file="$runtime/run/gui-model.id"
log_file="$runtime/logs/gui-model.log"
model_id="$GUI_MODEL_ID"
served_name="$GUI_MODEL_SERVED_NAME"
gpu_util="$GUI_MODEL_GPU_MEMORY_UTILIZATION"
min_free_mib="$GUI_MODEL_MIN_FREE_MIB"
max_model_len="$GUI_MODEL_MAX_MODEL_LEN"
model_dir="$GUI_MODEL_ROOT/$model_id"

if [[ ! -x "$venv/bin/vllm" ]]; then
  echo 'The isolated vLLM environment is missing. Run bootstrap first.' >&2
  exit 1
fi
if [[ ! -s "$model_dir/config.json" ]]; then
  echo "The pinned model snapshot is missing: $model_dir" >&2
  exit 1
fi

if [[ -f "$pid_file" ]]; then
  existing_pid="$(cat "$pid_file")"
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    cmdline="$(tr '\0' ' ' < "/proc/$existing_pid/cmdline")"
    if [[ "$cmdline" == *'vllm'* && "$cmdline" == *"$model_dir"* ]]; then
      echo "GUI model service is already running with PID $existing_pid." >&2
      exit 3
    fi
    echo "PID file points to an unrelated live process; refusing to overwrite it." >&2
    exit 4
  fi
  rm -f "$pid_file" "$model_file"
fi

free_mib="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -n 1 | tr -d '[:space:]')"
if [[ ! "$free_mib" =~ ^[0-9]+$ ]]; then
  echo 'Could not determine free GPU memory.' >&2
  exit 1
fi
if (( free_mib < min_free_mib )); then
  echo "Refusing to start $model_id: ${free_mib} MiB free, ${min_free_mib} MiB required." >&2
  echo 'No other GPU process was stopped.' >&2
  exit 5
fi

pixel_args="{\"size\":{\"longest_edge\":$GUI_MODEL_LONGEST_EDGE,\"shortest_edge\":$GUI_MODEL_SHORTEST_EDGE}}"
limit_mm_args="{\"image\":$GUI_MODEL_MAX_IMAGES}"
mkdir -p "$runtime/run" "$runtime/logs"
export HF_HOME="$runtime/cache/huggingface"
export VLLM_WORKER_MULTIPROC_METHOD=spawn
# WSL does not expose the CUDA UVA/pinned-memory path required by vLLM's V2
# runner, and FlashInfer's sampler would otherwise JIT-compile with nvcc.
export VLLM_USE_V2_MODEL_RUNNER=0
export VLLM_USE_FLASHINFER_SAMPLER=0

nohup "$venv/bin/vllm" serve "$model_dir" \
  --served-model-name "$served_name" \
  --host "$GUI_MODEL_HOST" \
  --port "$GUI_MODEL_PORT" \
  --api-key "$GUI_MODEL_API_KEY" \
  --dtype bfloat16 \
  --max-model-len "$max_model_len" \
  --max-num-seqs "$GUI_MODEL_MAX_NUM_SEQS" \
  --enforce-eager \
  --gpu-memory-utilization "$gpu_util" \
  --mm-processor-kwargs "$pixel_args" \
  --limit-mm-per-prompt "$limit_mm_args" \
  > "$log_file" 2>&1 &
pid=$!
echo "$pid" > "$pid_file"
echo "$model_id" > "$model_file"

echo "Starting GUI model '$model_id' with PID $pid."
echo "Log: $log_file"
for _ in $(seq 1 180); do
  if ! kill -0 "$pid" 2>/dev/null; then
    echo 'GUI model service exited during startup.' >&2
    tail -n 120 "$log_file" >&2 || true
    rm -f "$pid_file" "$model_file"
    exit 6
  fi
  if curl -fsS -H "Authorization: Bearer $GUI_MODEL_API_KEY" \
      "http://$GUI_MODEL_HOST:$GUI_MODEL_PORT/v1/models" >/dev/null 2>&1; then
    echo "GUI model API is ready: http://$GUI_MODEL_HOST:$GUI_MODEL_PORT/v1"
    exit 0
  fi
  sleep 2
done

echo 'Timed out waiting for the GUI model API; the process is still running for inspection.' >&2
tail -n 120 "$log_file" >&2 || true
exit 7
