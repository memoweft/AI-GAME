#!/usr/bin/env bash
set -Eeuo pipefail

config=/mnt/f/AI-GAME/config/model-runtime.env
# shellcheck disable=SC1090
source "$config"
runtime="$AI_GAME_RUNTIME_ROOT"
pid_file="$runtime/run/gui-model.pid"
model_file="$runtime/run/gui-model.id"
model_dir="$GUI_MODEL_ROOT/$GUI_MODEL_ID"

if [[ ! -f "$pid_file" ]]; then
  echo 'GUI model service is not recorded as running.'
  exit 0
fi
pid="$(cat "$pid_file")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  echo 'Removing a stale GUI model PID file.'
  rm -f "$pid_file" "$model_file"
  exit 0
fi
cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
if [[ "$cmdline" != *'vllm'* || "$cmdline" != *"$model_dir"* ]]; then
  echo "PID $pid does not match the managed GUI model service; refusing to signal it." >&2
  exit 4
fi

kill -TERM "$pid"
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pid_file" "$model_file"
    echo 'GUI model service stopped gracefully.'
    exit 0
  fi
  sleep 1
done

echo 'The service did not exit after 60 seconds. It was not force-killed.' >&2
exit 8
