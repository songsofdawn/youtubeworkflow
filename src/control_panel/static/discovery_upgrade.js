(() => {
  "use strict";

  const nativeFetch = window.fetch.bind(window);
  const qs = (selector, root = document) => root.querySelector(selector);

  function ensureCss() {
    if (document.querySelector('link[href="/discovery_upgrade.css"]')) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "/discovery_upgrade.css";
    document.head.appendChild(link);
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  async function requestJson(url, options = {}) {
    const response = await nativeFetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || payload.message || `HTTP ${response.status}`);
    }
    return payload;
  }

  function currentRankingMode() {
    const select = qs("#discoveryRankingMode");
    return select?.value === "potential" ? "potential" : "hot";
  }

  window.fetch = async function patchedFetch(input, init = {}) {
    const rawUrl = typeof input === "string" ? input : input?.url || "";
    let pathname = "";
    try {
      pathname = new URL(rawUrl, window.location.href).pathname;
    } catch {
      pathname = rawUrl;
    }
    if (
      pathname === "/api/discover" &&
      String(init.method || "GET").toUpperCase() === "POST" &&
      typeof init.body === "string"
    ) {
      try {
        const body = JSON.parse(init.body);
        body.ranking_mode = currentRankingMode();
        body.discovery_scope = qs("#discoveryScope")?.value || "auto";
        body.search_strength = qs("#discoverySearchStrength")?.value || "standard";
        init = { ...init, body: JSON.stringify(body) };
      } catch {}
    }
    return nativeFetch(input, init);
  };

  function updateModeHint() {
    const mode = currentRankingMode();
    const hint = qs("#discoveryModeHint");
    if (hint) {
      hint.textContent =
        mode === "hot"
          ? "热门：速度、播放量、新鲜度和互动率主导，Qwen 提供质量底线。"
          : "内容潜力：Qwen 内容价值、用户匹配、新颖性和本地化价值主导；热度只作辅助信号。";
    }
    try {
      localStorage.setItem("youtubeWorkflow.discoveryRankingMode", mode);
    } catch {}
  }

  function setupMode() {
    const select = qs("#discoveryRankingMode");
    if (!select) return;
    try {
      const saved = localStorage.getItem("youtubeWorkflow.discoveryRankingMode");
      if (saved === "hot" || saved === "potential") select.value = saved;
    } catch {}
    select.addEventListener("change", updateModeHint);
    updateModeHint();
  }

  let editorPacks = [];

  function splitDimensions(text) {
    return String(text || "")
      .split(/\n|,|，|\|/g)
      .map((v) => v.trim().toLowerCase())
      .filter(Boolean);
  }

  function packEditorMarkup(pack, index) {
    const dimensions = [["entities", "主题实体"], ["formats", "共享内容形式 ID"], ["positive_traits", "正向内容特征"], ["negative_intents", "负向意图"]];
    return `
      <article class="discovery-pack-editor-card" data-pack-index="${index}">
        <div class="discovery-pack-editor-card-head">
          <strong>领域 ${index + 1}</strong>
          <button class="button button-ghost button-small discovery-pack-delete" type="button" data-index="${index}">删除领域</button>
        </div>
        <div class="discovery-pack-editor-grid">
          <label class="field">
            <span>领域 ID</span>
            <input data-field="id" value="${escapeHtml(pack.id)}" maxlength="64" pattern="[a-z0-9_]{2,64}" required>
            <small>仅小写字母、数字、下划线。</small>
          </label>
          <label class="field">
            <span>显示名称</span>
            <input data-field="label" value="${escapeHtml(pack.label)}" maxlength="80" required>
          </label>
          <label class="field discovery-pack-editor-wide">
            <span>领域说明</span>
            <input data-field="description" value="${escapeHtml(pack.description)}" maxlength="240" required>
          </label>
          ${dimensions.map(([field, label]) => `<label class="field discovery-pack-editor-wide"><span>${label}（每行一项）</span><textarea data-field="${field}" rows="4">${escapeHtml((pack[field] || []).join("\n"))}</textarea></label>`).join("")}
          <small>共享形式：experiment、challenge、full_process、simulation、comparison、restoration、extreme_survival、build、transformation、investigation、documentary、mod_showcase、100_days。查询由下载偏好和搜索表现动态生成。</small>
          <label class="confirm-line discovery-pack-editor-wide">
            <input data-field="default_selected" type="checkbox" ${pack.default_selected !== false ? "checked" : ""}>
            <span>控制面板打开时默认选中此领域</span>
          </label>
        </div>
      </article>
    `;
  }

  function ensureEditorDialog() {
    let dialog = qs("#discoveryPackEditorDialog");
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.id = "discoveryPackEditorDialog";
    dialog.className = "discovery-pack-editor-dialog";
    dialog.innerHTML = `
      <form method="dialog" class="discovery-pack-editor-shell" id="discoveryPackEditorForm">
        <div class="discovery-pack-editor-header">
          <div>
            <h2>编辑 Discovery Next 领域</h2>
            <p>定义实体、形式与内容特征；保存后用于下一轮发现策略。</p>
          </div>
          <button class="button button-ghost" id="closeDiscoveryPackEditor" type="button">关闭</button>
        </div>
        <div id="discoveryPackEditorList" class="discovery-pack-editor-list"></div>
        <div class="discovery-pack-editor-footer">
          <button class="button button-ghost" id="addDiscoveryPack" type="button">＋ 新增领域</button>
          <div class="discovery-pack-editor-actions">
            <button class="button button-ghost" id="cancelDiscoveryPackEditor" type="button">取消</button>
            <button class="button button-primary" id="saveDiscoveryPacks" type="submit">保存领域配置</button>
          </div>
        </div>
      </form>
    `;
    document.body.appendChild(dialog);

    const close = () => dialog.close();
    qs("#closeDiscoveryPackEditor", dialog).addEventListener("click", close);
    qs("#cancelDiscoveryPackEditor", dialog).addEventListener("click", close);

    qs("#addDiscoveryPack", dialog).addEventListener("click", () => {
      const id = `custom_${Date.now().toString(36)}`;
      editorPacks.push({
        id,
        label: "新领域",
        description: "自定义发现领域",
        entities: [],
        formats: ["experiment"],
        positive_traits: [],
        negative_intents: [],
        default_selected: true,
      });
      renderEditor();
      const cards = dialog.querySelectorAll(".discovery-pack-editor-card");
      cards[cards.length - 1]?.scrollIntoView({ behavior: "smooth", block: "center" });
    });

    qs("#discoveryPackEditorList", dialog).addEventListener("click", (event) => {
      const button = event.target.closest(".discovery-pack-delete");
      if (!button) return;
      const index = Number(button.dataset.index);
      if (!Number.isInteger(index) || index < 0 || index >= editorPacks.length) return;
      const name = editorPacks[index]?.label || editorPacks[index]?.id || `领域 ${index + 1}`;
      if (!window.confirm(`确定删除“${name}”吗？保存后才会写入配置。`)) return;
      editorPacks.splice(index, 1);
      renderEditor();
    });

    qs("#discoveryPackEditorList", dialog).addEventListener("input", syncEditorState);
    qs("#discoveryPackEditorList", dialog).addEventListener("change", syncEditorState);

    qs("#discoveryPackEditorForm", dialog).addEventListener("submit", async (event) => {
      event.preventDefault();
      syncEditorState();
      if (!editorPacks.length) {
        window.alert("至少保留一个领域。");
        return;
      }
      const button = qs("#saveDiscoveryPacks", dialog);
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "保存中…";
      try {
        const packs = editorPacks.map((pack) => ({
          id: String(pack.id || "").trim().toLowerCase(),
          label: String(pack.label || "").trim(),
          description: String(pack.description || "").trim(),
          enabled: true,
          default_selected: pack.default_selected !== false,
          entities: pack.entities || [],
          formats: pack.formats || [],
          positive_traits: pack.positive_traits || [],
          negative_intents: pack.negative_intents || [],
        }));
        const payload = await requestJson("/api/discovery/packs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ packs }),
        });
        editorPacks = Array.isArray(payload.packs) ? payload.packs : packs;
        dialog.close();
        if (typeof window.loadDiscoveryCatalog === "function") {
          await window.loadDiscoveryCatalog();
        } else if (typeof loadDiscoveryCatalog === "function") {
          await loadDiscoveryCatalog();
        } else {
          window.location.reload();
        }
      } catch (error) {
        window.alert(`保存失败：${error.message || error}`);
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    });

    return dialog;
  }

  function syncEditorState() {
    const list = qs("#discoveryPackEditorList");
    if (!list) return;
    list.querySelectorAll(".discovery-pack-editor-card").forEach((card) => {
      const index = Number(card.dataset.packIndex);
      const pack = editorPacks[index];
      if (!pack) return;
      card.querySelectorAll("[data-field]").forEach((input) => {
        const field = input.dataset.field;
        if (field === "default_selected") {
          pack[field] = Boolean(input.checked);
        } else if (["entities", "formats", "positive_traits", "negative_intents"].includes(field)) {
          pack[field] = splitDimensions(input.value);
        } else {
          pack[field] = input.value;
        }
      });
    });
  }

  function renderEditor() {
    const list = qs("#discoveryPackEditorList");
    if (!list) return;
    list.innerHTML = editorPacks.map(packEditorMarkup).join("");
  }

  async function openEditor() {
    const dialog = ensureEditorDialog();
    const list = qs("#discoveryPackEditorList", dialog);
    list.innerHTML = '<p class="muted">正在读取领域配置…</p>';
    dialog.showModal();
    try {
      const payload = await requestJson("/api/discovery/packs?details=1");
      editorPacks = Array.isArray(payload.packs)
        ? payload.packs.map((pack) => ({
            ...pack,
          }))
        : [];
      renderEditor();
    } catch (error) {
      list.innerHTML = `<p class="error-text">读取失败：${escapeHtml(error.message || error)}</p>`;
    }
  }

  function setupEditor() {
    const button = qs("#editDiscoveryPacks");
    if (!button) return;
    button.addEventListener("click", openEditor);
  }

  function updateDiscoveryQuotaEstimate() {
    const checked = document.querySelectorAll(
      '#discoveryPacks input[type="checkbox"]:checked'
    ).length;
    const configuredMax = Number(qs("#discoveryMaxSearchRequests")?.value || 96);
    const globalMax = Math.max(1, Math.min(configuredMax, 100));
    let node = document.querySelector("#discoveryQuotaEstimate");
    if (!node) {
      node = document.createElement("small");
      node.id = "discoveryQuotaEstimate";
      const hint = document.querySelector("#discoveryModeHint");
      if (hint?.parentNode) {
        hint.parentNode.insertBefore(node, hint.nextSibling);
      }
    }
    if (node) {
      node.textContent =
        `已选 ${checked} 个领域；全局最多 ${globalMax} 次搜索。预算按学习偏好、真实产出和探索方向动态分配，没有每领域固定调用数。`;
    }
  }

  function setupDiscoveryQuotaEstimate() {
    const list = document.querySelector("#discoveryPacks");
    if (list) {
      list.addEventListener("change", updateDiscoveryQuotaEstimate);
    }
    qs("#discoveryMaxSearchRequests")?.addEventListener("input", updateDiscoveryQuotaEstimate);
    setTimeout(updateDiscoveryQuotaEstimate, 900);
  }

  function setupDurationDefaults() {
    const minInput = qs("#discoveryMinDurationMinutes");
    const maxInput = qs("#discoveryMaxDurationMinutes");
    if (minInput) minInput.max = "180";
    if (maxInput) {
      maxInput.max = "180";
      setTimeout(() => {
        const value = Number(maxInput.value || 0);
        if (value === 180 && !sessionStorage.getItem("discoveryMaxDurationTouched")) {
          maxInput.value = "120";
        }
      }, 800);
      maxInput.addEventListener("input", () => {
        sessionStorage.setItem("discoveryMaxDurationTouched", "1");
      });
    }
  }

  ensureCss();
  // Keep historical DOM IDs for settings serialization; Next does not use these controls.
  ["discoveryEmbeddingModel", "discoveryEmbeddingEnabled", "discoveryQueryPlanningEnabled",
    "discoveryVisualEnabled", "discoveryVisualTopN", "discoveryRecallTarget", "discoveryMetadataBatchSize"].forEach((id) => {
    const input = qs(`#${id}`);
    if (input) {
      input.disabled = true;
      input.closest("label")?.classList.add("hidden");
    }
  });
  setupMode();
  const scope = qs("#discoveryScope");
  const syncScope = () => qs("#discoveryManualFocus")?.classList.toggle("hidden", scope?.value !== "manual");
  scope?.addEventListener("change", syncScope); syncScope();
  setupEditor();
  setupDurationDefaults();
  setupDiscoveryQuotaEstimate();
})();
