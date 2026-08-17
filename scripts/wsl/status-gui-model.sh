#!/usr/bin/env bash
set -Eeuo pipefail

config=/mnt/f/AI-GAME/config/model-runtime.env
# shellcheck disable=SC1090
source "$config"
runtime="$AI_GAME_RUNTIME_ROOT"
pid_file="$runtime/run/gui-model.pid"
model_file="$runtime/run/gui-model.id"
model_dir="$GUI_MODEL_ROOT/$GUI_MODEL_ID"

status=stopped
pid=''
model_id=''
api_ready=false
if [[ -f "$pid_file" ]]; then
  pid="$(cat "$pid_file")"
  model_id="$(cat "$model_file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    if [[ "$cmdline" == *'vllm'* && "$cmdline" == *"$model_dir"* ]]; then
      status=running
      if curl -fsS -H "Authorization: Bearer $GUI_MODEL_API_KEY" \
          "http://$GUI_MODEL_HOST:$GUI_MODEL_PORT/v1/models" >/dev/null 2>&1; then
        api_ready=true
      fi
    else
      status=pid_mismatch
    fi
  else
    status=stale_pid
  fi
fi

free_mib="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -n 1 | tr -d '[:space:]')"
printf 'status=%s\n' "$status"
printf 'pid=%s\n' "$pid"
printf 'model_id=%s\n' "$model_id"
printf 'api_ready=%s\n' "$api_ready"
printf 'endpoint=http://%s:%s/v1\n' "$GUI_MODEL_HOST" "$GUI_MODEL_PORT"
printf 'gpu_free_mib=%s\n' "$free_mib"
if [[ -f "$runtime/run/environment-versions.txt" ]]; then
  echo 'environment:'
  sed 's/^/  /' "$runtime/run/environment-versions.txt"
fi
