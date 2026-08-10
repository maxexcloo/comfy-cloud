#!/usr/bin/env bash
set -euo pipefail

pod_env="$(jq -cn \
  --arg api_key "${API_KEY:?set API_KEY}" \
  --arg hf_token "${HF_TOKEN:-}" \
  --arg maximum_pending_generations "${MAXIMUM_PENDING_GENERATIONS:-8}" \
  --arg maximum_request_bytes "${MAXIMUM_REQUEST_BYTES:-104857600}" \
  --arg password "${COMFY_UI_PASSWORD:?set COMFY_UI_PASSWORD}" \
  --arg profiles "${MODEL_PROFILES:-}" \
  --arg username "${COMFY_UI_USERNAME:-comfy}" \
  '{API_KEY: $api_key, COMFY_UI_PASSWORD: $password, COMFY_UI_USERNAME: $username, JOBS_DIR: "/opt/ComfyUI/models/.comfy-control/jobs", MAXIMUM_PENDING_GENERATIONS: $maximum_pending_generations, MAXIMUM_REQUEST_BYTES: $maximum_request_bytes, MODE: "pod"}
   + if $profiles == "" then {} else {MODEL_PROFILES: $profiles} end
   + if ($hf_token == "" or $hf_token == "REPLACE_ME") then {} else {HF_TOKEN: $hf_token} end')"
storage_args=(--volume-in-gb "${RUNPOD_VOLUME_GB:-100}")
if [[ -n "${RUNPOD_NETWORK_VOLUME_ID:-}" && "${RUNPOD_NETWORK_VOLUME_ID}" != "REPLACE_ME" ]]; then
  storage_args=(--network-volume-id "${RUNPOD_NETWORK_VOLUME_ID}")
fi
runpodctl pod create \
  --container-disk-in-gb "${RUNPOD_CONTAINER_DISK_GB:-30}" \
  --env "${pod_env}" \
  --gpu-id "${RUNPOD_GPU_TYPE:?set RUNPOD_GPU_TYPE}" \
  --image "${IMAGE_NAME:?set IMAGE_NAME}" \
  --name comfy-control \
  --ports 8000/http \
  --volume-mount-path /opt/ComfyUI/models \
  "${storage_args[@]}"
