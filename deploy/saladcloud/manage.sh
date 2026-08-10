#!/usr/bin/env bash
set -euo pipefail

base_url="https://api.salad.com/api/public/organizations/${SALAD_ORGANISATION:?set SALAD_ORGANISATION}"
group="${SALAD_CONTAINER_GROUP:-comfy-control}"
headers=(--header "Salad-Api-Key: ${SALAD_API_KEY:?set SALAD_API_KEY}")

case "${1:-}" in
  deploy)
    project="${SALAD_PROJECT:?set SALAD_PROJECT}"
    jq \
      --arg api_key "${API_KEY:?set API_KEY}" \
      --arg group "${group}" \
      --arg hf_token "${HF_TOKEN:-}" \
      --arg image "${SALAD_IMAGE_NAME:-${IMAGE_NAME:?set IMAGE_NAME}}" \
      --arg maximum_pending_generations "${MAXIMUM_PENDING_GENERATIONS:-8}" \
      --arg maximum_request_bytes "${MAXIMUM_REQUEST_BYTES:-104857600}" \
      --arg password "${COMFY_UI_PASSWORD:?set COMFY_UI_PASSWORD}" \
      --arg priority "${SALAD_PRIORITY:-low}" \
      --arg profiles "${MODEL_PROFILES:-}" \
      --arg username "${COMFY_UI_USERNAME:-comfy}" \
      --argjson gpu_classes "${SALAD_GPU_CLASSES:?set SALAD_GPU_CLASSES to a JSON array}" \
      --argjson replicas "${SALAD_REPLICAS:-1}" \
      '.name = $group
       | .container.image = $image
       | .container.priority = $priority
       | .container.resources.gpu_classes = $gpu_classes
       | .container.environment_variables.API_KEY = $api_key
       | .container.environment_variables.COMFY_UI_PASSWORD = $password
       | .container.environment_variables.COMFY_UI_USERNAME = $username
       | .container.environment_variables.HF_TOKEN = $hf_token
       | .container.environment_variables.JOBS_DIR = "/opt/ComfyUI/models/.comfy-control/jobs"
       | .container.environment_variables.MAXIMUM_PENDING_GENERATIONS = $maximum_pending_generations
       | .container.environment_variables.MAXIMUM_REQUEST_BYTES = $maximum_request_bytes
       | .replicas = $replicas
       | if $profiles == "" then del(.container.environment_variables.MODEL_PROFILES) else .container.environment_variables.MODEL_PROFILES = $profiles end
       | if ($hf_token == "" or $hf_token == "REPLACE_ME") then del(.container.environment_variables.HF_TOKEN) else . end' \
      deploy/saladcloud/container-group.json \
    | curl --fail-with-body --silent --show-error \
      --request POST \
      --url "${base_url}/projects/${project}/containers" \
      --header "Content-Type: application/json" \
      "${headers[@]}" \
      --data-binary @- \
    | jq
    ;;
  destroy)
    project="${SALAD_PROJECT:?set SALAD_PROJECT}"
    curl --fail-with-body --silent --show-error \
      --request DELETE \
      --url "${base_url}/projects/${project}/containers/${group}" \
      "${headers[@]}"
    ;;
  gpu-classes)
    curl --fail-with-body --silent --show-error \
      --url "${base_url}/gpu-classes" \
      "${headers[@]}" \
    | jq '.items | sort_by(.name) | map({id, name, prices})'
    ;;
  logs)
    project="${SALAD_PROJECT:?set SALAD_PROJECT}"
    end_time="$(python -c 'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')"
    start_time="$(python -c 'from datetime import UTC, datetime, timedelta; print((datetime.now(UTC) - timedelta(hours=1)).isoformat())')"
    jq -cn \
      --arg end_time "${end_time}" \
      --arg group "${group}" \
      --arg project "${project}" \
      --arg start_time "${start_time}" \
      '{end_time: $end_time, page_size: 100, query: ("resource.type = \"container\" and resource.labels.project_name = \"" + $project + "\" and resource.labels.container_group_name = \"" + $group + "\""), sort_order: "desc", start_time: $start_time}' \
    | curl --fail-with-body --silent --show-error \
      --request POST \
      --url "${base_url}/log-entries" \
      --header "Content-Type: application/json" \
      "${headers[@]}" \
      --data-binary @- \
    | jq -r '.items[] | [.time, (.text_log // (.json_log | tojson))] | @tsv'
    ;;
  start | stop)
    project="${SALAD_PROJECT:?set SALAD_PROJECT}"
    curl --fail-with-body --silent --show-error \
      --request POST \
      --url "${base_url}/projects/${project}/containers/${group}/${1}" \
      "${headers[@]}"
    ;;
  status)
    project="${SALAD_PROJECT:?set SALAD_PROJECT}"
    curl --fail-with-body --silent --show-error \
      --url "${base_url}/projects/${project}/containers/${group}" \
      "${headers[@]}" \
    | jq
    ;;
  *)
    echo "usage: $0 {deploy|destroy|gpu-classes|logs|start|status|stop}" >&2
    exit 2
    ;;
esac
