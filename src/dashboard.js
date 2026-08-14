const formatDate = (timestamp) =>
  new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(Number(timestamp) * 1000));

for (const time of document.querySelectorAll("[data-timestamp]")) {
  time.textContent = formatDate(time.dataset.timestamp);
}

for (const control of document.querySelectorAll("[data-current]")) {
  if (control.type === "checkbox") {
    control.checked = control.dataset.current === "True";
  } else {
    control.value = control.dataset.current;
  }
}

for (const button of document.querySelectorAll("[data-confirmation]")) {
  button.closest("form").addEventListener("submit", (event) => {
    if (!confirm(button.dataset.confirmation)) event.preventDefault();
  });
}

for (const form of document.querySelectorAll("[data-live-filter]")) {
  let timer;
  const submit = () => {
    clearTimeout(timer);
    form.requestSubmit();
  };
  form.addEventListener("input", (event) => {
    if (event.target.matches('input[type="search"], input[name="q"]')) {
      clearTimeout(timer);
      timer = setTimeout(() => form.requestSubmit(), 300);
    }
  });
  form.addEventListener("change", submit);
}

for (const editor of document.querySelectorAll("[data-route-editor]")) {
  const value = editor.querySelector("[data-route-value]");
  const syncModel = (target) => {
    const provider = target.querySelector("[data-route-provider]").value;
    const model = target.querySelector("[data-route-model]");
    for (const option of model.options) {
      const available = option.dataset.providers.trim().split(/\s+/);
      option.disabled = !available.includes(provider);
      option.hidden = option.disabled;
    }
    if (model.selectedOptions[0]?.disabled)
      model.value = [...model.options].find(
        (option) => !option.disabled,
      )?.value;
  };
  const serialise = () => {
    for (const target of editor.querySelectorAll("[data-route-target]"))
      syncModel(target);
    value.value = JSON.stringify(
      Object.fromEntries(
        [...editor.querySelectorAll("[data-route-key]")].map((route) => [
          route.dataset.routeKey,
          [...route.querySelectorAll("[data-route-target]")].map((target) => ({
            model: target.querySelector("[data-route-model]").value,
            provider: target.querySelector("[data-route-provider]").value,
          })),
        ]),
      ),
    );
    const selectedModels = new Set(
      [...editor.querySelectorAll("[data-route-model]")].map(
        (select) => select.value,
      ),
    );
    for (const model of selectedModels) {
      const installation = document.querySelector(
        `[data-model-package="${CSS.escape(model)}"]`,
      );
      if (installation && !installation.disabled) installation.checked = true;
    }
    for (const route of editor.querySelectorAll("[data-route-key]")) {
      const selected = [...route.querySelectorAll("[data-route-provider]")].map(
        (select) => select.value,
      );
      for (const select of route.querySelectorAll("[data-route-provider]")) {
        for (const option of select.options)
          option.disabled =
            option.value !== select.value && selected.includes(option.value);
      }
    }
  };
  let dragged;
  editor.addEventListener("dragstart", (event) => {
    dragged = event.target.closest("[data-route-target]");
    if (dragged) event.dataTransfer.effectAllowed = "move";
  });
  editor.addEventListener("dragover", (event) => {
    const target = event.target.closest("[data-route-target]");
    if (!dragged || !target || target === dragged) return;
    event.preventDefault();
    const bounds = target.getBoundingClientRect();
    target.parentElement.insertBefore(
      dragged,
      event.clientY < bounds.top + bounds.height / 2
        ? target
        : target.nextSibling,
    );
  });
  editor.addEventListener("dragend", () => {
    dragged = undefined;
    serialise();
  });
  editor.addEventListener("change", serialise);
  editor.addEventListener("click", (event) => {
    const route = event.target.closest("[data-route-key]");
    if (!route) return;
    const target = event.target.closest("[data-route-target]");
    if (event.target.closest("[data-route-add]")) {
      const row = editor
        .querySelector(`[data-route-template="${route.dataset.routeKey}"]`)
        .content.firstElementChild.cloneNode(true);
      const used = new Set(
        [...route.querySelectorAll("[data-route-provider]")].map(
          (select) => select.value,
        ),
      );
      const select = row.querySelector("[data-route-provider]");
      const available = [...select.options].find(
        (option) => !used.has(option.value),
      );
      if (!available) return;
      select.value = available.value;
      route.querySelector("[data-route-list]").append(row);
    } else if (event.target.closest("[data-route-remove]")) {
      target.remove();
    } else if (event.target.closest("[data-route-up]")) {
      if (target.previousElementSibling)
        target.parentElement.insertBefore(
          target,
          target.previousElementSibling,
        );
    } else if (event.target.closest("[data-route-down]")) {
      if (target.nextElementSibling)
        target.parentElement.insertBefore(target.nextElementSibling, target);
    } else {
      return;
    }
    serialise();
  });
  serialise();
}

const providerRefresh = document.querySelector("[data-provider-refresh]");
if (providerRefresh) {
  providerRefresh.setAttribute("aria-busy", "true");
  const url = new URL(window.location);
  url.searchParams.set("telemetry", "true");
  fetch(url)
    .then((response) => {
      if (!response.ok)
        throw new Error("Provider Telemetry Could Not Be Loaded");
      return response.text();
    })
    .then((html) => {
      const refreshed = new DOMParser().parseFromString(html, "text/html");
      for (const source of refreshed.querySelectorAll("#provider-rows > tr")) {
        const target = document.getElementById(source.id);
        for (const selector of [
          "[data-provider-status]",
          "[data-provider-usage]",
        ]) {
          const current = target?.querySelector(selector);
          const replacement = source.querySelector(selector);
          if (current && replacement) current.replaceWith(replacement);
        }
      }
    })
    .catch(() => {})
    .finally(() => providerRefresh.removeAttribute("aria-busy"));
}

const closeDialogOnBackdrop = (dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
};

for (const button of document.querySelectorAll("[data-provider-settings]")) {
  const dialog = document.getElementById(
    `provider-settings-${button.dataset.providerSettings}`,
  );
  button.addEventListener("click", () => dialog.showModal());
  for (const close of dialog.querySelectorAll("[data-close]"))
    close.addEventListener("click", () => dialog.close());
  closeDialogOnBackdrop(dialog);
}

const logDialog = document.getElementById("log-dialog");
if (logDialog) {
  const logBody = logDialog.querySelector(".dialog-body");
  const logFollowState = document.getElementById("log-follow-state");
  const logOutput = document.getElementById("log-output");
  let following = true;
  let logSource;
  const updateLogFollowState = () => {
    logFollowState.textContent = following ? "Following" : "Paused";
  };
  const scrollLogsToBottom = () => {
    if (!following) return;
    requestAnimationFrame(() => {
      logBody.scrollTop = logBody.scrollHeight;
    });
  };
  logBody.addEventListener("scroll", () => {
    following =
      logBody.scrollHeight - logBody.scrollTop - logBody.clientHeight <= 24;
    updateLogFollowState();
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-log-url]");
    if (!button) return;
    event.preventDefault();
    if (logSource) logSource.close();
    document.getElementById("log-title").textContent =
      `${button.dataset.logProvider} Logs`;
    following = true;
    updateLogFollowState();
    logOutput.textContent = "Connecting…";
    logDialog.showModal();
    scrollLogsToBottom();
    logSource = new EventSource(button.dataset.logUrl);
    logSource.onmessage = (message) => {
      const data = JSON.parse(message.data);
      const lines = [...data.entries]
        .reverse()
        .map(
          (item) =>
            `${formatDate(item.created_at)} ${item.source || "Provider"} ${item.level.toUpperCase()} ${item.message}${item.request_id ? ` [${item.request_id}]` : ""}`,
        );
      if (data.worker_error)
        lines.push(`Worker Logs Unavailable: ${data.worker_error}`);
      logOutput.textContent = lines.join("\n");
      scrollLogsToBottom();
    };
    logSource.onerror = () => {
      if (!logOutput.textContent) logOutput.textContent = "Waiting For Logs…";
    };
  });
  logDialog
    .querySelector("[data-close]")
    .addEventListener("click", () => logDialog.close());
  logDialog.addEventListener("close", () => {
    if (logSource) {
      logSource.close();
      logSource = undefined;
    }
  });
  closeDialogOnBackdrop(logDialog);
}

const deployDialog = document.getElementById("deploy-dialog");
if (deployDialog) {
  const deployDetail = document.getElementById("deploy-option-detail");
  const deployForm = document.getElementById("deploy-form");
  const deployGpu = document.getElementById("deploy-gpu");
  const deployMemory = document.getElementById("deploy-memory");
  let deploymentOptions = [];
  const updateDeployDetail = () => {
    const option = deploymentOptions.find(
      (item) => item.id === deployGpu.value,
    );
    deployMemory.value = option?.memory_gb || "";
    if (!option) {
      deployDetail.textContent = "The Provider Will Choose Automatically.";
      return;
    }
    const details = [
      option.type
        ? option.type
            .replaceAll("-", " ")
            .replace(/\b\w/g, (letter) => letter.toUpperCase())
        : null,
      option.available ? "Available" : "Currently Unavailable",
      option.compatible ? "Model Compatible" : "Insufficient GPU Memory",
      option.memory_gb ? `${option.memory_gb} GB GPU Memory` : null,
      option.cpu_cores ? `${option.cpu_cores} vCPU` : null,
      option.system_memory_gb
        ? `${option.system_memory_gb} GB System Memory`
        : null,
      option.cloud ? `${option.cloud} Cloud` : null,
      option.location || null,
      Number.isFinite(option.reliability)
        ? `${(Number(option.reliability) * 100).toFixed(1)}% Reliability`
        : null,
      Number.isFinite(option.cost_per_hour)
        ? `$${Number(option.cost_per_hour).toFixed(3)} Per Hour`
        : "Provider-Managed Pricing",
    ].filter(Boolean);
    deployDetail.textContent = details.join(" · ");
  };
  for (const button of document.querySelectorAll("[data-deploy-provider]")) {
    button.addEventListener("click", async () => {
      document.getElementById("deploy-title").textContent =
        `Deploy ${button.dataset.deployProvider}`;
      deployForm.action = button.dataset.deployAction;
      deployGpu.replaceChildren(new Option("Loading Availability…", ""));
      deployGpu.disabled = true;
      deployDialog.showModal();
      const response = await fetch(button.dataset.deployOptions);
      if (!response.ok) {
        deployDetail.textContent = "Live Availability Could Not Be Loaded.";
        deployGpu.replaceChildren(new Option("Automatic", ""));
        deployGpu.disabled = false;
        return;
      }
      deploymentOptions = (await response.json()).options;
      deployGpu.replaceChildren(
        new Option("Automatic (Recommended)", ""),
        ...deploymentOptions.map((item) => {
          const option = new Option(
            `${item.label}${Number.isFinite(item.cost_per_hour) ? ` · $${Number(item.cost_per_hour).toFixed(3)}/h` : ""}`,
            item.id,
            false,
            false,
          );
          option.disabled = !item.available || !item.compatible;
          return option;
        }),
      );
      deployGpu.disabled = false;
      updateDeployDetail();
    });
  }
  deployGpu.addEventListener("change", updateDeployDetail);
  for (const button of deployDialog.querySelectorAll("[data-close]"))
    button.addEventListener("click", () => deployDialog.close());
  closeDialogOnBackdrop(deployDialog);
}

const addFilter = document.getElementById("add-filter");
if (addFilter) {
  addFilter.addEventListener("click", () => {
    const path = document.getElementById("filter-path").value;
    const operator = document.getElementById("filter-operator").value;
    const value = document.getElementById("filter-value").value.trim();
    if (!path || !value) return;
    const chip = document.createElement("span");
    chip.className = "filter-chip";
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "filter";
    input.value = `${path}|${operator}|${value}`;
    chip.append(input, `${path} ${operator.replaceAll("_", " ")} ${value} `);
    const remove = document.createElement("button");
    remove.className = "btn btn-ghost-secondary btn-sm";
    remove.type = "button";
    remove.dataset.removeFilter = "";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "Remove Filter");
    chip.append(remove);
    document.getElementById("filter-chips").append(chip);
    document.getElementById("filter-value").value = "";
    document.getElementById("media-search").requestSubmit();
  });
  document.getElementById("filter-chips").addEventListener("click", (event) => {
    if (event.target.matches("[data-remove-filter]")) {
      event.target.closest(".filter-chip").remove();
      document.getElementById("media-search").requestSubmit();
    }
  });
}

const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const labelledValue = (label, value) => {
  const item = element("div", "card card-body");
  item.append(
    element("small", "d-block text-secondary", label),
    element("strong", "d-block", value || "—"),
  );
  return item;
};

const formatBytes = (bytes) => {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${bytes || 0} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = bytes;
  let unit = "B";
  for (const candidate of units) {
    value /= 1024;
    unit = candidate;
    if (value < 1024) break;
  }
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)} ${unit}`;
};

const formatDuration = (seconds) => {
  if (!Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds % 60)} s`;
};

const mediaDialog = document.getElementById("media-dialog");
if (mediaDialog) {
  const detailRoot = document.getElementById("media-detail");
  const nextButton = mediaDialog.querySelector("[data-next-media]");
  const previousButton = mediaDialog.querySelector("[data-previous-media]");
  let activeAssetId = null;
  let openRequest = 0;
  const availableMediaIds = () => [
    ...new Set(
      [...document.querySelectorAll("[data-media-id]:not(.related-item)")].map(
        (item) => item.dataset.mediaId,
      ),
    ),
  ];
  const updateNavigation = () => {
    const disabled = availableMediaIds().length < 2;
    nextButton.disabled = disabled;
    previousButton.disabled = disabled;
  };
  const openMedia = async (assetId, updateAddress = true) => {
    activeAssetId = String(assetId);
    const request = ++openRequest;
    updateNavigation();
    detailRoot.replaceChildren(element("p", "", "Loading…"));
    if (!mediaDialog.open) mediaDialog.showModal();
    const response = await fetch(`/media/${assetId}`);
    if (request !== openRequest) return;
    if (!response.ok) {
      detailRoot.replaceChildren(
        element("p", "error", "Media Could Not Be Loaded."),
      );
      return;
    }
    const item = await response.json();
    const use = item.uses[0] || {};
    const actualModel = use.provider_model || use.model;
    document.getElementById("media-kind").textContent = item.content_type;
    document.getElementById("media-title").textContent =
      actualModel || `Media ${item.id}`;
    const preview = item.content_type.startsWith("video/")
      ? element("video", "media-preview")
      : element("img", "media-preview");
    preview.src = `/media/${item.id}/content`;
    if (preview.tagName === "VIDEO") preview.controls = true;
    else preview.alt = use.prompt || "Generated Media";

    const facts = element("div", "detail-grid");
    facts.append(
      labelledValue("Provider", use.provider),
      labelledValue("Model", actualModel),
      labelledValue(
        "Dimensions",
        item.width && item.height ? `${item.width} × ${item.height}` : "—",
      ),
      labelledValue("Size", formatBytes(item.size)),
      labelledValue("Generation Time", formatDuration(use.generation_seconds)),
    );
    const body = document.createDocumentFragment();
    body.append(preview, facts);
    for (const generation of item.uses) {
      const section = element("section", "card card-body detail-section");
      const heading = element(
        "div",
        "align-items-center d-flex justify-content-between",
      );
      heading.append(element("h3", "", "Generation Details"));
      const historyLink = element("a", "", "View History");
      historyLink.href = `/history?q=${encodeURIComponent(generation.history_id)}`;
      const links = element("div", "d-flex gap-2");
      if (generation.source_url) {
        const sourceLink = element("a", "", "View Source");
        sourceLink.href = generation.source_url;
        sourceLink.rel = "noopener noreferrer";
        sourceLink.target = "_blank";
        links.append(sourceLink);
      }
      links.append(historyLink);
      heading.append(links);
      section.append(heading);
      if (generation.prompt) {
        section.append(
          element("small", "", "Prompt"),
          element("p", "prompt", generation.prompt),
        );
      }
      section.append(element("small", "", "Parameters"));
      const parameters = element(
        "pre",
        "parameters",
        JSON.stringify(generation.parameters, null, 2),
      );
      section.append(parameters);
      body.append(section);
    }
    const related = [
      ...item.lineage.sources.map((entry) => ["Source", entry]),
      ...item.lineage.derivatives.map((entry) => ["Derivative", entry]),
    ];
    if (related.length) {
      const section = element("section", "card card-body detail-section");
      section.append(element("h3", "", "Related Media"));
      const list = element("div", "d-flex flex-wrap gap-2");
      for (const [relationship, entry] of related) {
        const button = element(
          "button",
          "btn btn-outline-secondary btn-sm related-item",
          `${relationship}: ${entry.filename || `Media ${entry.id}`}`,
        );
        button.type = "button";
        button.dataset.mediaId = entry.id;
        list.append(button);
      }
      section.append(list);
      body.append(section);
    }
    detailRoot.replaceChildren(body);
    if (updateAddress) {
      const url = new URL(window.location);
      url.searchParams.set("asset", item.id);
      history.pushState({ asset: item.id }, "", url);
    }
  };
  const browseMedia = (direction) => {
    const ids = availableMediaIds();
    if (ids.length < 2) return;
    const currentIndex = ids.indexOf(activeAssetId);
    const baseIndex =
      currentIndex === -1 ? (direction > 0 ? -1 : 0) : currentIndex;
    openMedia(ids[(baseIndex + direction + ids.length) % ids.length]);
  };
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-media-id]");
    if (target) openMedia(target.dataset.mediaId);
  });
  mediaDialog
    .querySelector("[data-close]")
    .addEventListener("click", () => mediaDialog.close());
  previousButton.addEventListener("click", () => browseMedia(-1));
  nextButton.addEventListener("click", () => browseMedia(1));
  closeDialogOnBackdrop(mediaDialog);
  document.addEventListener("keydown", (event) => {
    if (!mediaDialog.open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      mediaDialog.close();
      return;
    }
    if (event.target.matches("input, select, textarea, [contenteditable]"))
      return;
    if (event.key === "ArrowLeft") browseMedia(-1);
    if (event.key === "ArrowRight") browseMedia(1);
  });
  mediaDialog.addEventListener("close", () => {
    activeAssetId = null;
    openRequest += 1;
    const url = new URL(window.location);
    if (url.searchParams.has("asset")) {
      url.searchParams.delete("asset");
      history.pushState({}, "", url);
    }
  });
  const initialAsset = new URL(window.location).searchParams.get("asset");
  updateNavigation();
  if (initialAsset) openMedia(initialAsset, false);
}
