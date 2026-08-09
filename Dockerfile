FROM nvidia/cuda:13.3.1-cudnn-runtime-ubuntu24.04

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ARG COMFYUI_REF=v0.31.1
ARG MODEL_PROFILE
ENV BUILTIN_CATALOGUE_DIR=/opt/comfy-cloud/catalogue \
    COMFYUI_DIR=/opt/ComfyUI \
    COMFYUI_URL=http://127.0.0.1:8188 \
    DEBIAN_FRONTEND=noninteractive \
    HF_XET_HIGH_PERFORMANCE=1 \
    MODE=pod \
    PATH=/opt/venv/bin:${PATH} \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv

RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg git python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/* && \
    uv venv "${VIRTUAL_ENV}"

RUN git clone --filter=blob:none --branch "${COMFYUI_REF}" --depth 1 \
      https://github.com/Comfy-Org/ComfyUI.git /opt/ComfyUI \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" --requirements /opt/ComfyUI/requirements.txt

WORKDIR /opt/comfy-cloud
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY catalogue ./catalogue
COPY deploy ./deploy
COPY profiles ./profiles
RUN uv export --frozen --no-dev --extra build --extra s3 --extra vast --no-emit-project \
      --no-hashes --output-file /tmp/requirements.txt \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" --requirements /tmp/requirements.txt \
    && uv build --wheel --out-dir /tmp/dist \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" --no-deps /tmp/dist/*.whl \
    && rm -rf /tmp/dist /tmp/requirements.txt

RUN --mount=type=secret,id=HF_TOKEN,env=HF_TOKEN \
    --mount=type=secret,id=CIVITAI_TOKEN,env=CIVITAI_TOKEN \
    if [ -n "${MODEL_PROFILE}" ]; then \
      comfy-cloud models-fetch "/opt/comfy-cloud/profiles/${MODEL_PROFILE}.yaml" \
        --models-dir /opt/ComfyUI/models; \
    fi

EXPOSE 8000
CMD ["/opt/venv/bin/python", "-m", "comfy_cloud.supervisor"]
