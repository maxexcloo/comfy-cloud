#!/usr/bin/env bash
set -euo pipefail

shell_quote() {
  python -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$1"
}

environment="-e API_KEY=$(shell_quote "${API_KEY:?set API_KEY}") -e COMFY_UI_PASSWORD=$(shell_quote "${COMFY_UI_PASSWORD:?set COMFY_UI_PASSWORD}") -e COMFY_UI_USERNAME=$(shell_quote "${COMFY_UI_USERNAME:-comfy}") -e JOBS_DIR=/opt/ComfyUI/models/.comfy-control/jobs -e MAXIMUM_PENDING_GENERATIONS=$(shell_quote "${MAXIMUM_PENDING_GENERATIONS:-8}") -e MAXIMUM_REQUEST_BYTES=$(shell_quote "${MAXIMUM_REQUEST_BYTES:-104857600}") -e MODE=pod -p 8000:8000"
if [[ -n "${HF_TOKEN:-}" && "${HF_TOKEN}" != "REPLACE_ME" ]]; then
  environment="${environment} -e HF_TOKEN=$(shell_quote "${HF_TOKEN}")"
fi
if [[ -n "${MODEL_PROFILES:-}" ]]; then
  environment="${environment} -e MODEL_PROFILES=$(shell_quote "${MODEL_PROFILES}")"
fi
vastai --api-key "${VAST_API_KEY:?set VAST_API_KEY}" create instance \
  "${VAST_OFFER_ID:?set VAST_OFFER_ID from mise run vast:offers}" \
  --disk "${VAST_DISK_GB:-100}" \
  --env "${environment}" \
  --image "${IMAGE_NAME:?set IMAGE_NAME}" \
  --label comfy-control
