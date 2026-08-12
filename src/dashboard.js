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

const logDialog = document.getElementById("log-dialog");
if (logDialog) {
  const logOutput = document.getElementById("log-output");
  let logSource;
  for (const button of document.querySelectorAll("[data-log-url]")) {
    button.addEventListener("click", () => {
      if (logSource) logSource.close();
      document.getElementById("log-title").textContent =
        `${button.dataset.logProvider} Logs`;
      logOutput.textContent = "Connecting…";
      logDialog.showModal();
      logSource = new EventSource(button.dataset.logUrl);
      logSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        logOutput.textContent = data.entries
          .map(
            (item) =>
              `${formatDate(item.created_at)} ${item.level.toUpperCase()} ${item.message}${item.request_id ? ` [${item.request_id}]` : ""}`,
          )
          .join("\n");
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
    if (logSource) logSource.close();
  });
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
  });
  document.getElementById("filter-chips").addEventListener("click", (event) => {
    if (event.target.matches("[data-remove-filter]"))
      event.target.closest(".filter-chip").remove();
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

const mediaDialog = document.getElementById("media-dialog");
if (mediaDialog) {
  const detailRoot = document.getElementById("media-detail");
  const openMedia = async (assetId, updateAddress = true) => {
    detailRoot.replaceChildren(element("p", "", "Loading…"));
    mediaDialog.showModal();
    const response = await fetch(`/media/${assetId}`);
    if (!response.ok) {
      detailRoot.replaceChildren(
        element("p", "error", "Media Could Not Be Loaded."),
      );
      return;
    }
    const item = await response.json();
    const use = item.uses[0] || {};
    document.getElementById("media-kind").textContent = item.content_type;
    document.getElementById("media-title").textContent =
      use.model || `Media ${item.id}`;
    const preview = item.content_type.startsWith("video/")
      ? element("video", "media-preview")
      : element("img", "media-preview");
    preview.src = `/media/${item.id}/content`;
    if (preview.tagName === "VIDEO") preview.controls = true;
    else preview.alt = use.prompt || "Generated Media";

    const facts = element("div", "detail-grid");
    facts.append(
      labelledValue("Model", use.model),
      labelledValue("Operation", use.operation?.replaceAll("_", " ")),
      labelledValue("Provider", use.provider),
      labelledValue("Created", formatDate(item.created_at)),
      labelledValue(
        "Dimensions",
        item.width && item.height ? `${item.width} × ${item.height}` : "—",
      ),
      labelledValue(
        "Size",
        new Intl.NumberFormat(undefined, {
          style: "unit",
          unit: "byte",
          unitDisplay: "narrow",
        }).format(item.size),
      ),
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
    const related = [...item.lineage.sources, ...item.lineage.derivatives];
    if (related.length) {
      const section = element("section", "detail-section");
      section.append(element("h3", "", "Related Media"));
      const list = element("div", "related-media");
      for (const entry of related) {
        const button = element(
          "button",
          "related-item",
          entry.filename || `Media ${entry.id}`,
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
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-media-id]");
    if (target) openMedia(target.dataset.mediaId);
  });
  mediaDialog
    .querySelector("[data-close]")
    .addEventListener("click", () => mediaDialog.close());
  mediaDialog.addEventListener("close", () => {
    const url = new URL(window.location);
    if (url.searchParams.has("asset")) {
      url.searchParams.delete("asset");
      history.pushState({}, "", url);
    }
  });
  const initialAsset = new URL(window.location).searchParams.get("asset");
  if (initialAsset) openMedia(initialAsset, false);
}
