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

const closeDialogOnBackdrop = (dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
};

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
  for (const button of document.querySelectorAll("[data-log-url]")) {
    button.addEventListener("click", (event) => {
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
      logSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
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
  }
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
    const option = deploymentOptions.find((item) => item.id === deployGpu.value);
    deployMemory.value = option?.memory_gb || "";
    if (!option) {
      deployDetail.textContent = "The provider will choose automatically.";
      return;
    }
    const details = [
      option.available ? "Available" : "Currently Unavailable",
      option.memory_gb ? `${option.memory_gb} GB GPU Memory` : null,
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
        deployDetail.textContent = "Live availability could not be loaded.";
        deployGpu.replaceChildren(new Option("Automatic", ""));
        deployGpu.disabled = false;
        return;
      }
      deploymentOptions = (await response.json()).options;
      deployGpu.replaceChildren(
        new Option("Automatic (Recommended)", ""),
        ...deploymentOptions.map(
          (item) =>
            new Option(
              `${item.label}${Number.isFinite(item.cost_per_hour) ? ` · $${Number(item.cost_per_hour).toFixed(3)}/h` : ""}`,
              item.id,
              false,
              false,
            ),
        ),
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
  const item = element("div", "detail-value");
  item.append(element("small", "", label), element("strong", "", value || "—"));
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
      const section = element("section", "detail-section");
      const heading = element("div", "section-head");
      heading.append(element("h3", "", "Generation Details"));
      const historyLink = element("a", "", "View History");
      historyLink.href = `/history?q=${encodeURIComponent(generation.history_id)}`;
      heading.append(historyLink);
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
      const section = element("section", "detail-section");
      section.append(element("h3", "", "Related Media"));
      const list = element("div", "related-media");
      for (const [relationship, entry] of related) {
        const button = element(
          "button",
          "related-item",
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
    const baseIndex = currentIndex === -1 ? (direction > 0 ? -1 : 0) : currentIndex;
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
    if (event.target.matches("input, select, textarea, [contenteditable]")) return;
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
