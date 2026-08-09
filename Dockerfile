FROM nvidia/cuda:13.3.1-cudnn-runtime-ubuntu24.04@sha256:2c9730db1d78ce3a7503a2f4ff2d64add3e7d1a47d57da504376192dda335242

COPY --from=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /usr/local/bin/uv

# ComfyUI v0.31.1. Keep the release label and immutable commit in sync.
ARG COMFYUI_REF=fe4195f7f4275f2626cbafc703acc3ddde1e5490
ARG MODEL_PROFILE
ENV BUILTIN_CATALOGUE_DIR=/opt/comfy-control/catalogue \
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

RUN git init /opt/ComfyUI \
    && git -C /opt/ComfyUI remote add origin https://github.com/Comfy-Org/ComfyUI.git \
    && git -C /opt/ComfyUI fetch --depth 1 origin "${COMFYUI_REF}" \
    && git -C /opt/ComfyUI checkout --detach FETCH_HEAD \
    && uv pip install --python "${VIRTUAL_ENV}/bin/python" --requirements /opt/ComfyUI/requirements.txt

WORKDIR /opt/comfy-control
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
      comfy-control models-fetch "/opt/comfy-control/profiles/${MODEL_PROFILE}.yaml" \
        --models-dir /opt/ComfyUI/models; \
    fi

EXPOSE 8000
CMD ["/opt/venv/bin/python", "-m", "comfy_control.supervisor"]
