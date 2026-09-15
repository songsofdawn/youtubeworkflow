"use strict";

const state = {
  dashboard: null,
  searchResults: [],
  discoveryCatalog: [],
  discoveryPayload: null,
  selectedResults: new Set(),
  selectedTasks: new Set(),
  activeLogJob: null,
  publishTask: null,
  publishTitlePrefix: "",
  publishTitleBase: "",
  publishAutoTitle: true,
  publishDynamicAuto: true,
  publishPriorityDefault: false,
  publishPriority: false,
  renderReviewTask: null,
  coverPreviewTask: null,
  coverInitialized: false,
  refreshBusy: false,
  setupDismissed: false,
  setupManuallyOpened: false,
  llmInitialized: false,
  discoveryInitialized: false,
  publishingSettingsInitialized: false,
  dubbingInitialized: false,
  discoveryJobId: null,
  discoveryAutoRestorePending: true,
  automationAccountId: "",
};

const AUTOMATION_SETTINGS_KEY = "youtube-workflow.automation.v2";
const LEGACY_AUTOMATION_SETTINGS_KEY = "youtube-workflow.automation.v1";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 404 && path === "/api/tasks/redownload") {
      throw new Error("当前面板服务未加载重新下载接口，请完全退出并重新启动面板（不要只刷新网页）。");
    }
    throw new Error(payload.error || `请求失败（${response.status}）`);
  }
  return payload;
}

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

let toastTimer;
function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 3200);
}

function number(value) {
  return new Intl.NumberFormat("zh-CN", { notation: value > 999999 ? "compact" : "standard" }).format(value || 0);
}

function discoveryAge(value) {
  const hours = Math.max(0, Number(value) || 0);
  return hours >= 48 ? `${(hours / 24).toFixed(1)} 天` : `${hours.toFixed(1)} 小时`;
}

function formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function targetLabel(target) {
  const parts = String(target || "").split(/[\\/]/);
  return parts.at(-1) || target || "未命名任务";
}

function publishTargetTitle(target) {
  const label = targetLabel(target);
  const readable = label
    .replace(/^[A-Za-z0-9_-]{11}[_-]*/, "")
    .replace(/_+/g, " ")
    .trim();
  return readable || label;
}

function updatePublishDescriptionCount() {
  const field = $("#publishDescription");
  const counter = $("#publishDescriptionCount");
  if (!field || !counter) return;
  const bytes = new TextEncoder().encode(field.value).length;
  const max = 1900;
  counter.textContent = `投稿安全长度 ${bytes} / ${max}`;
  counter.classList.toggle("limit-warning", bytes >= max - 50);
}

function elapsedText(startedAt) {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

async function refreshDashboard(showError = false) {
  if (state.refreshBusy) return;
  state.refreshBusy = true;
  try {
    const dashboard = await api("/api/dashboard");
    state.dashboard = dashboard;
    renderDashboard(dashboard);
    await restoreLatestDiscoveryResult(dashboard.jobs || []);
    const connection = $("#connectionState");
    connection.className = "connection online";
    connection.innerHTML = "<i></i> 本地面板已连接";
  } catch (error) {
    const connection = $("#connectionState");
    connection.className = "connection offline";
    connection.innerHTML = "<i></i> 连接中断";
    if (showError) toast(error.message, true);
  } finally {
    state.refreshBusy = false;
  }
}

function renderDashboard(dashboard) {
  const summary = dashboard.summary;
  $("#metricTasks").textContent = summary.tasks;
  $("#metricQueued").textContent = summary.queued;
  $("#metricRunning").textContent = summary.running;
  $("#metricRendered").textContent = summary.rendered;
  $("#metricPublished").textContent = summary.published;
  renderHealth(dashboard.health);
  renderScheduler(dashboard.scheduler);
  renderPublishOrder(dashboard.scheduler?.publishing || {});
  renderTasks(dashboard.tasks);
  renderJobs(dashboard.jobs);
  if (state.coverPreviewTask && $("#coverPreviewDialog").open) updateCoverPreview();
  updateChineseSourceControls();
}

function automationSettingsSnapshot() {
  return {
    enabled: $("#autoPublishAfterDownload").checked,
    coverChoice: $("#automationCoverChoice").value,
    coverCloudAuthorized: $("#automationCoverCloudAuthorized").checked,
    target: $("#automationTarget").value,
    englishPolicy: $("#automationEnglishPolicy").value,
    chinesePolicy: $("#automationChinesePolicy").value,
    dubbingEnabled: $("#automationDubbingEnabled").checked,
    dubbingReferenceMode: $("#automationDubbingReferenceMode").value,
    dubbingReferenceStart: $("#automationDubbingReferenceStart").value,
    dubbingReferenceEnd: $("#automationDubbingReferenceEnd").value,
    dubbingSubtitleDisplay: $("#automationDubbingSubtitleDisplay").value,
    dubbingReviewPolicy: $("#automationDubbingReviewPolicy").value,
    renderMode: $("#automationRenderMode").value,
    failurePolicy: $("#automationFailurePolicy").value,
    silentVideoPolicy: $("#automationSilentVideoPolicy").value,
    metadataProvider: $("#automationMetadataProvider").value,
    accountId: $("#automationAccount").value || state.automationAccountId,
    onlySelf: $("#automationOnlySelf").checked,
  };
}

function saveAutomationSettings() {
  const settings = automationSettingsSnapshot();
  state.automationAccountId = settings.accountId || "";
  try {
    localStorage.setItem(AUTOMATION_SETTINGS_KEY, JSON.stringify(settings));
  } catch (_error) {
    // The workflow remains usable when browser storage is unavailable.
  }
}

function restoreAutomationSettings() {
  let settings = null;
  let migratedLegacySettings = false;
  try {
    settings = JSON.parse(localStorage.getItem(AUTOMATION_SETTINGS_KEY) || "null");
    if (!settings) {
      settings = JSON.parse(localStorage.getItem(LEGACY_AUTOMATION_SETTINGS_KEY) || "null");
      migratedLegacySettings = Boolean(settings);
    }
  } catch (_error) {
    settings = null;
  }
  if (!settings || typeof settings !== "object") return;
  if (migratedLegacySettings) {
    settings.dubbingReviewPolicy = "auto_fallback";
  }
  $("#autoPublishAfterDownload").checked = settings.enabled === true;
  $("#automationCoverChoice").value = ["off", "local", "cloud"].includes(settings.coverChoice) ? settings.coverChoice : "off";
  $("#automationCoverCloudAuthorized").checked = settings.coverChoice === "cloud" && settings.coverCloudAuthorized === true;
  const target = ["subtitles", "render", "publish"].includes(settings.target)
    ? settings.target
    : "publish";
  const englishPolicy = ["quality", "youtube_first", "whisper"].includes(settings.englishPolicy)
    ? settings.englishPolicy
    : settings.whisper === false ? "youtube_first" : "quality";
  const chinesePolicy = ["youtube_preferred", "api_always", "youtube_only"].includes(settings.chinesePolicy)
    ? settings.chinesePolicy
    : settings.translate === false ? "youtube_only" : "youtube_preferred";
  $("#automationTarget").value = target;
  $("#automationEnglishPolicy").value = englishPolicy;
  $("#automationChinesePolicy").value = chinesePolicy;
  $("#automationDubbingEnabled").checked = settings.dubbingEnabled === true;
  if (["auto", "manual"].includes(settings.dubbingReferenceMode)) {
    $("#automationDubbingReferenceMode").value = settings.dubbingReferenceMode;
  }
  $("#automationDubbingReferenceStart").value = settings.dubbingReferenceStart ?? "";
  $("#automationDubbingReferenceEnd").value = settings.dubbingReferenceEnd ?? "";
  if (["chinese", "bilingual"].includes(settings.dubbingSubtitleDisplay)) {
    $("#automationDubbingSubtitleDisplay").value = settings.dubbingSubtitleDisplay;
  }
  if (["auto_fallback", "block", "continue"].includes(settings.dubbingReviewPolicy)) {
    $("#automationDubbingReviewPolicy").value = settings.dubbingReviewPolicy;
  }
  if (["ass", "softsub", "hardsub", "both"].includes(settings.renderMode)) {
    $("#automationRenderMode").value = settings.renderMode;
  }
  if (["skip", "fail"].includes(settings.failurePolicy)) {
    // ``skip`` was removed from the UI.  Existing browser settings continue
    // as the original-media fallback path instead of silently skipping.
    $("#automationFailurePolicy").value = "fail";
  }
  if (["publish_original", "skip"].includes(settings.silentVideoPolicy)) {
    $("#automationSilentVideoPolicy").value = "publish_original";
  }
  if (["auto", "local_ollama", "translation_api"].includes(settings.metadataProvider)) {
    $("#automationMetadataProvider").value = settings.metadataProvider;
  }
  state.automationAccountId = String(settings.accountId || "");
  $("#automationOnlySelf").checked = settings.onlySelf === true;
}

function updateAutomationFlow() {
  const target = $("#automationTarget").value;
  const dubbingToggle = $("#automationDubbingEnabled");
  if (target === "subtitles") dubbingToggle.checked = false;
  const dubbingEnabled = dubbingToggle.checked;
  if (dubbingEnabled) {
    $("#automationChinesePolicy").value = "api_always";
    if ($("#automationRenderMode").value === "ass") {
      $("#automationRenderMode").value = "hardsub";
    }
  }
  if (
    target === "publish"
    && !["hardsub", "both"].includes($("#automationRenderMode").value)
  ) {
    $("#automationRenderMode").value = "hardsub";
  }
  const settings = automationSettingsSnapshot();
  const enabled = settings.enabled;
  const targetLabels = {
    subtitles: "双语字幕",
    render: settings.dubbingEnabled ? "中文配音成片" : "双语成片",
    publish: settings.onlySelf
      ? `${settings.dubbingEnabled ? "中配·" : ""}仅自己可见投稿`
      : `${settings.dubbingEnabled ? "中配·" : ""}公开投稿`,
  };
  $("#automationStateBadge").textContent = enabled ? "已启用" : "已关闭";
  $("#automationStateBadge").classList.toggle("enabled", enabled);
  $("#automationFlow").classList.toggle("inactive", !enabled);
  $("#automationMasterSummary").textContent = enabled
    ? `新下载的视频会自动处理到${targetLabels[settings.target]}`
    : "自动化关闭；新下载只保存素材，不生成中文封面";
  const noSpeechOutcome = settings.target === "publish"
    ? "无可靠语音时保留原视频并生成中文投稿信息"
    : "无可靠语音时保留失败状态";
  const englishDescriptions = {
    quality: `YouTube 字幕与 Whisper 自动比较，选择质量更高者；${noSpeechOutcome}`,
    youtube_first: `优先使用 YouTube 英文字幕；缺少时用 Whisper 兜底；${noSpeechOutcome}`,
    whisper: `忽略已有 YouTube 英文字幕，每个视频都强制使用本地 Whisper；${noSpeechOutcome}`,
  };
  const chineseDescriptions = {
    youtube_preferred: "优先可靠的 YouTube 中文；缺少或不可用时调用翻译 API",
    api_always: "忽略已有 YouTube 中文字幕；每个视频都调用所选 API 重新翻译",
    youtube_only: `只使用可靠的 YouTube 中文；缺少时${settings.target === "publish" ? "改用原视频投稿" : "保留失败"}`,
  };
  const renderDescriptions = {
    ass: "只生成双语 ASS 字幕文件，不编码视频",
    softsub: `生成可开关字幕的 MKV${settings.dubbingEnabled ? "并替换为中文配音音轨" : "并保留原音轨"}；执行硬性质检`,
    hardsub: `生成投稿用硬字幕 MP4${settings.dubbingEnabled ? "并替换为中文配音音轨" : "并保留原音轨"}；执行硬性质检`,
    both: `同时生成硬字幕 MP4 和软字幕 MKV${settings.dubbingEnabled ? "，两者使用中文配音音轨" : "，两者保留原音轨"}；执行硬性质检`,
  };
  $("#automationEnglishFlow").textContent = englishDescriptions[settings.englishPolicy];
  $("#automationChineseFlow").textContent = chineseDescriptions[settings.chinesePolicy];
  const referenceDescription = settings.dubbingReferenceMode === "manual"
    ? `${settings.dubbingReferenceStart || "?"}–${settings.dubbingReferenceEnd || "?"} 秒参考声音`
    : "自动选取 5–10 秒参考声音";
  $("#automationDubbingFlow").textContent = settings.dubbingEnabled
    ? `VoxCPM2 · ${referenceDescription} · ${settings.dubbingSubtitleDisplay === "chinese" ? "仅中文字幕" : "中英双语字幕"}`
    : "本流程不生成中配，成片保留原始音轨";
  $("#automationRenderFlow").textContent = renderDescriptions[settings.renderMode];
  const coverEnabled = settings.coverChoice !== "off";
  $("#automationCoverFlow").textContent = coverEnabled
    ? `${settings.coverChoice === "cloud" ? "API 文案 + Pillow" : "本地 Ollama + Pillow"}；失败时使用原封面，其他步骤继续`
    : "未启用；保留原始封面";
  const metadataLabel = $("#automationMetadataProvider").selectedOptions[0]?.textContent || "自动模型";
  const silentFlow = "；无配音视频保留原画面与音轨，生成中文投稿信息后投稿";
  $("#automationMetadataFlow").textContent = `${metadataLabel}；自动填写标题、标签、简介和分区${silentFlow}`;
  const accountLabel = $("#automationAccount").selectedOptions[0]?.textContent || "自动选择账号";
  $("#automationPublishFlow").textContent = `${accountLabel} · ${settings.onlySelf ? "仅自己可见" : "公开投稿"}；瞬时网络错误自动重试并切换线路`;
  const omittedStages = settings.target === "subtitles"
    ? new Set(["dubbing", "render", "metadata", "publish"])
    : settings.target === "render" ? new Set(["metadata", "publish"]) : new Set();
  if (!settings.dubbingEnabled) omittedStages.add("dubbing");
  if (!coverEnabled) omittedStages.add("cover");
  for (const stage of $$('[data-automation-stage]')) {
    stage.classList.toggle("omitted", omittedStages.has(stage.dataset.automationStage));
  }
  const renderDisabled = settings.target === "subtitles";
  $("#automationRenderMode").closest(".automation-setting").classList.toggle("is-disabled", renderDisabled);
  $("#automationRenderMode").disabled = renderDisabled;
  const dubbingAvailable = settings.target !== "subtitles";
  $("#automationDubbingEnabled").disabled = !dubbingAvailable;
  $("#automationDubbingEnabled").closest(".automation-setting").classList.toggle("is-disabled", !dubbingAvailable);
  const manualReference = settings.dubbingEnabled && settings.dubbingReferenceMode === "manual";
  $("#automationDubbingReferenceStartField").classList.toggle("hidden", !manualReference);
  $("#automationDubbingReferenceEndField").classList.toggle("hidden", !manualReference);
  for (const selector of ["#automationDubbingReferenceMode", "#automationDubbingSubtitleDisplay", "#automationDubbingReviewPolicy"]) {
    const control = $(selector);
    control.disabled = !settings.dubbingEnabled;
    control.closest(".automation-setting").classList.toggle("is-disabled", !settings.dubbingEnabled);
  }
  $("#automationDubbingReferenceStart").disabled = !manualReference;
  $("#automationDubbingReferenceEnd").disabled = !manualReference;
  const chinesePolicyLocked = settings.dubbingEnabled;
  $("#automationChinesePolicy").disabled = chinesePolicyLocked;
  $("#automationChinesePolicy").closest(".automation-setting").classList.toggle("is-disabled", chinesePolicyLocked);
  const dubbingReady = Boolean(state.dashboard?.health?.dubbing?.configured);
  $("#automationDubbingHint").textContent = settings.dubbingEnabled
    ? dubbingReady
      ? "环境已就绪；中配会在翻译后、成片前运行"
      : "环境未就绪；加入队列前会阻止并提示配置"
    : "默认关闭；启用后使用本地 VoxCPM2 替换音轨";
  for (const selector of ["#automationMetadataProvider", "#automationAccount", "#automationOnlySelf", "#automationSilentVideoPolicy"]) {
    const control = $(selector);
    const disabled = settings.target !== "publish";
    control.closest(".automation-setting").classList.toggle("is-disabled", disabled);
    control.disabled = disabled;
  }
  const reviewOutcome = !settings.dubbingEnabled
    ? ""
    : settings.dubbingReviewPolicy === "continue"
      ? " 中配时槽超限时仍会继续成片与投稿，请仅在接受重叠风险时使用。"
      : settings.dubbingReviewPolicy === "auto_fallback"
        ? " 中配无法安全适配时会自动改为原声中文字幕成片并继续投稿。"
      : ` 中配时槽超限会在成片前自动改用${settings.target === "publish" ? "原视频并继续投稿" : "原始音轨成片"}。`;
  $("#automationFailureFlow").textContent = (settings.target === "publish"
    ? "异常策略：字幕或排版无法安全成片时，保留原因并改用原视频，自动生成中文投稿信息和封面继续上传。"
    : "异常策略：无法安全完成字幕或成片时，将该视频保留为失败状态，不自动跳过。")
    + reviewOutcome
    + " 无可靠语音的视频按上方专用策略处理。";
}

function renderScheduler(scheduler) {
  const container = $("#schedulerSlots");
  if (!container) return;
  const resources = scheduler?.resources || {};
  const globalSlot = scheduler?.global || { running: 0, capacity: 0 };
  const publishing = scheduler?.publishing || {};
  const labels = [
    ["network", "下载"],
    ["gpu_heavy", "识别/成片"],
    ["paid_api", "AI API"],
    ["upload", "投稿"],
  ];
  const resourceSlots = labels.map(([key, label]) => {
    const slot = resources[key] || { running: 0, capacity: 0 };
    const active = Number(slot.running) > 0 ? " active" : "";
    return `<span class="scheduler-slot${active}">${label} ${Number(slot.running)}/${Number(slot.capacity)}</span>`;
  }).join("");
  const guardSlot = publishing.active
    ? `<span class="scheduler-slot guard" title="${escapeHtml(publishing.step || "投稿保护已启用")}">${escapeHtml(publishing.step || "投稿保护中")}</span>`
    : `<span class="scheduler-slot">投稿间隔 ${Math.max(1, Math.round(Number(publishing.minimum_interval_seconds || 180) / 60))} 分钟 · 今日 ${Number(publishing.completed_today || 0)}/${Number(publishing.daily_limit || 0) || "不限"}</span>`;
  container.innerHTML = resourceSlots
    + `<span class="scheduler-slot global">全局 ${Number(globalSlot.running)}/${Number(globalSlot.capacity)}</span>`
    + guardSlot;
}

function renderPublishOrder(publishing) {
  const container = $("#publishOrder .publish-order-list");
  if (!container) return;
  const queue = Array.isArray(publishing?.queue) ? publishing.queue : [];
  if (!queue.length) {
    container.innerHTML = '<p class="muted">暂无排队投稿</p>';
    return;
  }
  container.innerHTML = queue.map((job, index) => {
    const current = job.status === "running";
    const status = current
      ? "当前投稿"
      : String(job.step || "").includes("投稿保护") ? "等待冷却" : "等待中";
    const priority = Number(job.priority || 0) > 0
      ? '<span class="publish-order-priority">优先</span>'
      : "";
    const queuedBefore = queue.slice(0, index).some((item) => item.status !== "running");
    const queuedAfter = queue.slice(index + 1).some((item) => item.status !== "running");
    const controls = !current
      ? `<span class="publish-order-actions">
          <button class="button button-ghost button-small move-publish-job" type="button" data-job-id="${escapeHtml(job.id)}" data-direction="up" ${queuedBefore ? "" : "disabled"}>上移</button>
          <button class="button button-ghost button-small move-publish-job" type="button" data-job-id="${escapeHtml(job.id)}" data-direction="down" ${queuedAfter ? "" : "disabled"}>下移</button>
          <button class="button button-ghost button-small prioritize-job" type="button" data-job-id="${escapeHtml(job.id)}">置顶</button>
        </span>`
      : "";
    const directTitle = String(job.title || job.original_title || "").trim();
    const title = directTitle && !/^[A-Za-z0-9_-]{11}[_-]/.test(directTitle)
      ? directTitle
      : publishTargetTitle(job.target) || directTitle || job.video_id || "未命名投稿";
    return `<div class="publish-order-row${current ? " current" : ""}">
      <span class="publish-order-position">${index + 1}</span>
      <span class="publish-order-copy" title="${escapeHtml(title)}"><strong>${escapeHtml(title)}</strong></span>
      <span class="publish-order-status" title="${escapeHtml(job.step || status)}">${priority}${priority ? " · " : ""}${status}</span>
      ${controls}
    </div>`;
  }).join("");
}

function renderHealth(health) {
  const asr = health.asr || {};
  const asrMode = String(asr.device || "unknown").toLowerCase();
  const computeType = asr.compute_type || "unknown";
  const whisperLabel = asrMode === "cpu"
    ? `Whisper CPU / ${computeType}（可用，速度较慢）`
    : `Whisper GPU / ${computeType}（需要 NVIDIA 驱动）`;
  const activeProvider = health.llm?.providers?.find((item) => item.id === health.llm?.active?.provider);
  const labels = {
    python_runtime: "统一 Python 3.11 运行环境",
    tools: "yt-dlp / FFmpeg 工具",
    whisper_model: whisperLabel,
    youtube_api: "YouTube API（关键词搜索选配）",
    youtube_cookies: "YouTube Cookie（登录验证选配）",
    translation_api: `${activeProvider?.label || "AI"} API（翻译选配）`,
    discovery_llm: "Ollama 本地智能发现（选配）",
    biliup: "biliup 投稿工具",
    biliup_account: "哔哩哔哩账号（投稿选配）",
    dubbing_runtime: "中文配音独立运行时（选配）",
    voxcpm2_model: "VoxCPM2 本地模型（选配）",
  };
  $("#healthChecks").innerHTML = Object.entries(labels)
    .map(([key, label]) => `
      <div class="check-item ${health.checks[key] ? "ok" : ""}">
        <span>${label}</span><i aria-label="${health.checks[key] ? "已就绪" : "未就绪"}"></i>
      </div>
    `)
    .join("");
  const badge = $("#readinessBadge");
  badge.textContent = health.ready ? "核心环境就绪" : "需要配置";
  badge.classList.toggle("ready", health.ready);
  const dubbing = health.dubbing || {};
  const dubbingHint = $("#dubbingHealthHint");
  if (dubbingHint) {
    const dubbingMissing = dubbing.config_error
      || (!dubbing.runtime_ready
        ? "缺少独立 Python 运行时"
        : (!dubbing.demucs_ready || !dubbing.voxcpm_ready)
          ? "运行时缺少 Demucs / VoxCPM2 包"
          : dubbing.entrypoint_ready === false
            ? `配音入口无法加载${dubbing.runtime_error ? `：${dubbing.runtime_error}` : ""}`
            : dubbing.torchcodec_ready === false
              ? (dubbing.preflight_error || "TorchCodec / FFmpeg Shared 预检失败")
              : !dubbing.device_ready
                ? `PyTorch / ${dubbing.device || "cuda"} 不可用${dubbing.runtime_error ? `：${dubbing.runtime_error}` : ""}`
                : `缺少本地模型 ${dubbing.model_path || "models/VoxCPM2"}`);
    dubbingHint.textContent = dubbing.configured
      ? `配音环境已就绪 · ${dubbing.device || "cuda"} · ${dubbing.model_path || "VoxCPM2"}`
      : `配音环境未就绪：${dubbingMissing}`;
    dubbingHint.classList.toggle("ready", Boolean(dubbing.configured));
  }
  if (!state.dubbingInitialized) {
    $("#dubbingEnabled").checked = Boolean(dubbing.enabled_by_default);
    updateDubbingControls();
    state.dubbingInitialized = true;
  }
  const automationAccount = $("#automationAccount");
  if (automationAccount) {
    const previous = automationAccount.value || state.automationAccountId;
    const accounts = health.publishing?.accounts || [];
    automationAccount.innerHTML = accounts.length
      ? accounts.map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.label)}</option>`).join("")
      : '<option value="">请先登录哔哩哔哩</option>';
    automationAccount.value = accounts.some((account) => account.id === previous)
      ? previous
      : accounts[0]?.id || "";
    state.automationAccountId = automationAccount.value;
    updateAutomationFlow();
  }
  renderSetupGuide(health);
}

function coverRequestValues(automated) {
  const prefix = automated ? "automation" : "manual";
  const choice = $("#" + prefix + "CoverChoice").value;
  if (choice === "off") {
    return { cover_choice: "off", cover_enabled: false, cover_cloud_authorized: false };
  }
  if (!state.dashboard?.health?.cover?.per_job_options) {
    throw new Error("后台尚不支持分区封面开关，请在活动任务完成后重启后台再提交");
  }
  const authorized = $("#" + prefix + "CoverCloudAuthorized").checked;
  if (choice === "cloud" && !state.dashboard?.health?.cover?.api_key_configured) {
    throw new Error("API 文案封面缺少当前翻译供应商的 Key；请先在‘配置服务 → AI 翻译’保存 Key，或关闭封面生成／改用本地模式");
  }
  if (choice === "cloud" && !authorized) {
    const authorization = $("#" + prefix + "CoverCloudAuthorized");
    authorization.scrollIntoView({ behavior: "smooth", block: "center" });
    authorization.focus({ preventScroll: true });
    throw new Error(`已选择${automated ? "自动化" : "手动"} API 封面；请勾选封面文案 API 授权，或关闭封面生成／改用本地模式`);
  }
  return { cover_choice: choice, cover_cloud_authorized: choice === "cloud" && authorized };
}

function updateCoverRequestControls(health = state.dashboard?.health) {
  for (const prefix of ["automation", "manual"]) {
    const cloud = $("#" + prefix + "CoverChoice").value === "cloud";
    const authorization = $("#" + prefix + "CoverCloudAuthorized");
    // Consent is a user choice, independent of asynchronous service readiness.
    authorization.disabled = !cloud;
    if (!cloud) authorization.checked = false;
    const hint = !cloud
      ? "仅选择 API 文案模式时需要授权；关闭或本地模式无需授权。"
      : !health
        ? "可先勾选授权；正在读取当前翻译供应商的配置状态。"
        : !health.cover?.api_key_configured
          ? "可先勾选授权；使用前需在“配置服务 → AI 翻译”保存 Key，也可关闭封面或改用本地模式。"
          : "只发送标题与限长摘要，可能收费；Pillow 在本地绘图。";
    authorization.title = hint;
    $("#" + prefix + "CoverAuthorizationHint").textContent = hint;
  }
}

function automationRequestValues(autoPublish, settings = automationSettingsSnapshot()) {
  if (!autoPublish) return { auto_publish: false };
  return {
    auto_publish: true,
    automation_target: settings.target,
    english_subtitle_policy: settings.englishPolicy,
    automation_chinese_policy: settings.chinesePolicy,
    whisper_for_auto_subtitles: settings.englishPolicy !== "youtube_first",
    auto_translate_missing: settings.chinesePolicy !== "youtube_only",
    publish_metadata_provider: settings.metadataProvider,
    account_id: settings.accountId,
    publish_only_self: settings.onlySelf,
    automation_render_mode: settings.renderMode,
    automation_failure_policy: settings.failurePolicy,
    automation_silent_video_policy: settings.silentVideoPolicy,
    automation_dubbing_review_policy: settings.dubbingReviewPolicy,
    dubbing_enabled: settings.dubbingEnabled,
    dubbing_reference_mode: settings.dubbingReferenceMode,
    dubbing_reference_start: settings.dubbingReferenceStart,
    dubbing_reference_end: settings.dubbingReferenceEnd,
    dubbing_subtitle_display: settings.dubbingSubtitleDisplay,
    force_dubbing: false,
  };
}

function downloadRequestValues() {
  if (!$("#autoPublishAfterDownload").checked) {
    return { auto_publish: false, cover_choice: "off", cover_enabled: false, cover_cloud_authorized: false };
  }
  return { ...automationRequestValues(true), ...coverRequestValues(true) };
}

function updateSetupStatus(selector, configured, readyText, missingText) {
  const element = $(selector);
  element.textContent = configured ? readyText : missingText;
  element.classList.toggle("configured", configured);
}

function renderSetupGuide(health) {
  updateCoverRequestControls(health);
  const checks = health.checks || {};
  updateSetupStatus("#youtubeSetupStatus", checks.youtube_api, "已配置", "未配置");
  updateSetupStatus("#youtubeCookiesSetupStatus", checks.youtube_cookies, "已导入", "未导入");
  updateSetupStatus("#translationSetupStatus", checks.translation_api, "已配置", "未配置");
  updateSetupStatus("#discoveryLlmSetupStatus", checks.discovery_llm, "已连接", health.discovery?.enabled ? "未连接" : "未启用");
  updateSetupStatus("#biliupSetupStatus", checks.biliup_account, "已登录", "未登录");
  renderLlmSettings(health.llm);
  renderDiscoverySettings(health.discovery);
  renderPublishingSettings(health.publishing);
  const coverSupported = Boolean(health.cover);
  for (const selector of ["#coverEnabled", "#coverAllowPaidCopy", "#saveCoverSettings"]) {
    $(selector).disabled = !coverSupported;
  }
  $("#coverSettingsHint").textContent = coverSupported
    ? "保存服务配置后，单独重新生成封面会使用这里的模式；下方处理任务使用各自区域的封面选项，普通下载不生成中文封面。"
    : "后台仍是旧版本。请等待下载、渲染和投稿任务完成后，关闭旧面板并重新运行 start_panel.bat，再刷新页面；仅刷新网页不能更新后台。";
  if (!coverSupported) state.coverInitialized = false;
  if (coverSupported && !state.coverInitialized) {
    $("#coverEnabled").checked = Boolean(health.cover?.enabled);
    $("#coverAllowPaidCopy").checked = Boolean(health.cover?.allow_paid_copy);
    $("#coverMode").value = health.cover?.mode || "local";
    $("#coverAllowCloudApi").checked = Boolean(health.cover?.allow_cloud_api);
    state.coverInitialized = true;
  }
  $("#coverMode").disabled = !health.cover?.modes?.includes("cloud");
  $("#coverCloudKeyStatus").textContent = health.cover?.api_key_configured
    ? `将使用 ${health.cover.api_provider_label || "当前翻译供应商"} · ${health.cover.api_model || "当前模型"}`
    : "当前翻译供应商尚未配置 Key";
  $("#coverCloudFields").classList.toggle("hidden", $("#coverMode").value !== "cloud");
  if (coverSupported && !health.cover?.modes?.includes("cloud")) {
    $("#coverSettingsHint").textContent = "后台尚不支持双模式；请在任务完成后重启后台，才能选择 API 文案 + Pillow。";
  }
  updateSetupStatus("#coverSetupStatus", health.cover?.enabled && health.cover?.pillow_ready,
    "已启用", !coverSupported ? "需重启后台" : health.cover?.enabled ? "缺少 Pillow" : "未启用");
  if (health.cover?.enabled && health.cover?.mode === "cloud") {
    const cloudReady = health.cover.pillow_ready && health.cover.api_key_configured && health.cover.allow_cloud_api;
    updateSetupStatus("#coverSetupStatus", cloudReady, "API + Pillow 已启用",
      !health.cover.pillow_ready ? "缺少 Pillow" : !health.cover.api_key_configured ? "缺少翻译 API Key" : "等待 API 授权");
  }
  const hasMissingOption = !checks.youtube_api || !checks.youtube_cookies || !checks.translation_api || !checks.biliup_account;
  const shouldShow = state.setupManuallyOpened || (hasMissingOption && !state.setupDismissed);
  $("#setupGuide").classList.toggle("hidden", !shouldShow);
}

function renderPublishingSettings(publishing) {
  if (!publishing || state.publishingSettingsInitialized) return;
  const minutes = Number(publishing.publish_min_interval_minutes);
  $("#publishMinIntervalMinutes").value = Number.isInteger(minutes) && minutes >= 1
    ? minutes
    : 3;
  $("#defaultPublishTitlePrefix").value = publishing.default_title_prefix || "";
  state.publishingSettingsInitialized = true;
}

function renderDiscoverySettings(discovery) {
  if (!discovery || state.discoveryInitialized) return;
  $("#discoveryLlmEnabled").checked = Boolean(discovery.enabled);
  $("#discoveryOllamaBaseUrl").value = discovery.base_url || "http://127.0.0.1:11434";
  $("#discoveryOllamaModel").value = discovery.model || "qwen3.5:9b";
  $("#discoveryEmbeddingModel").value = discovery.embedding_model || "qwen3-embedding:0.6b";
  $("#discoveryEmbeddingEnabled").checked = Boolean(discovery.embedding_enabled);
  $("#discoveryQueryPlanningEnabled").checked = Boolean(discovery.query_planning_enabled);
  $("#discoveryVisualEnabled").checked = Boolean(discovery.visual_enabled);
  $("#discoveryThinking").checked = Boolean(discovery.thinking);
  $("#discoveryRecallTarget").value = discovery.recall_target || 1000;
  $("#discoveryMaxSearchRequests").value = discovery.max_search_requests || 100;
  $("#discoveryMetadataMaxCandidates").value = discovery.metadata_max_candidates || 100;
  $("#discoveryMinDurationMinutes").value = discovery.minimum_duration_minutes || 5;
  $("#discoveryMinDurationMinutes").max = discovery.maximum_duration_minutes || 45;
  $("#discoveryMaxDurationMinutes").value = discovery.maximum_duration_minutes || 45;
  $("#discoveryMaxDurationMinutes").max = discovery.maximum_duration_minutes || 45;
  $("#discoveryMetadataBatchSize").value = discovery.metadata_batch_size || 10;
  $("#discoveryVisualTopN").value = discovery.visual_top_n ?? 24;
  $("#discoveryTimeoutSeconds").value = discovery.timeout_seconds || 180;
  const feedback = discovery.feedback || {};
  $("#discoveryFeedbackSummary").textContent = feedback.total
    ? `已积累 ${feedback.total} 条偏好反馈；后续发现会自动用于排序。`
    : "尚无偏好反馈。模型只接收公开视频元数据和 YouTube 缩略图。";
  if (discovery.architecture === "discovery_next") {
    $("#discoveryFeedbackSummary").textContent = `已学习 ${discovery.learning_sample_size || 0} 个视频。下载计为中等正反馈；可在任务日志查看分析和画像更新。`;
  }
  state.discoveryInitialized = true;
}

function renderLlmSettings(llm) {
  if (!llm?.providers?.length || state.llmInitialized) return;
  const providerSelect = $("#translationProviderSelect");
  providerSelect.innerHTML = llm.providers.map((provider) =>
    `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.label)}${provider.configured ? " · 已存 Key" : ""}</option>`
  ).join("");
  providerSelect.value = llm.active.provider;
  updateLlmProviderFields(llm.active.provider, llm.active.model, llm.active.base_url);
  $("#translationThinkingSelect").value = llm.active.thinking;
  $("#translationBatchSize").value = llm.active.batch_size;
  $("#translationContextBefore").value = llm.active.context_before;
  $("#translationContextAfter").value = llm.active.context_after;
  $("#translationMaxOutputTokens").value = llm.active.max_output_tokens;
  state.llmInitialized = true;
}

function selectedLlmProvider() {
  const llm = state.dashboard?.health?.llm;
  return llm?.providers?.find((item) => item.id === $("#translationProviderSelect").value);
}

function updateLlmProviderFields(providerId, modelId = "", baseUrl = "", resetThinking = false) {
  const provider = state.dashboard?.health?.llm?.providers?.find((item) => item.id === providerId);
  if (!provider) return;
  const modelSelect = $("#translationModelSelect");
  modelSelect.innerHTML = provider.models.map((model) =>
    `<option value="${escapeHtml(model.id)}">${escapeHtml(model.label)}</option>`
  ).join("");
  modelSelect.value = provider.models.some((model) => model.id === modelId)
    ? modelId
    : provider.models[0].id;
  $("#translationCustomModelField").classList.toggle("hidden", !provider.custom_model);
  if (provider.custom_model) $("#translationCustomModelInput").value = modelId || "";
  $("#translationBaseUrlInput").value = baseUrl || provider.base_url;
  $("#translationApiKeyLabel").textContent = provider.key_env;
  $("#translationProviderHint").textContent = `${provider.label} · ${provider.configured ? "本机已保存 Key" : "尚未保存 Key"}`;
  updateSetupStatus("#translationSetupStatus", provider.configured, "已配置", "未配置");
  $("#translationThinkingSelect").disabled = !provider.thinking;
  if (resetThinking || !provider.thinking) {
    $("#translationThinkingSelect").value = provider.default_thinking || "disabled";
  }
}

const stageNames = { download: "下载", english: "英文", translation: "AI翻译", dubbing: "配音", render: "成片", cover: "封面", publish: "投稿" };

function renderTasks(tasks) {
  const taskKeys = new Set(tasks.map((task) => task.task));
  for (const selected of [...state.selectedTasks]) {
    if (!taskKeys.has(selected)) state.selectedTasks.delete(selected);
  }
  $("#selectedTaskCount").textContent = state.selectedTasks.size;
  const selectedRows = tasks.filter((task) => state.selectedTasks.has(task.task));
  const deletableCount = selectedRows.filter((task) => !task.active_job).length;
  const activeCount = selectedRows.length - deletableCount;
  const redownloadSelectedButton = $("#redownloadSelectedTasks");
  redownloadSelectedButton.disabled = deletableCount === 0;
  redownloadSelectedButton.textContent = deletableCount ? `重新下载 (${deletableCount})` : "重新下载";
  redownloadSelectedButton.title = "重新下载原始素材；运行中或排队中的项目会跳过";
  const deleteSelectedButton = $("#deleteSelectedTasks");
  deleteSelectedButton.disabled = deletableCount === 0;
  deleteSelectedButton.textContent = deletableCount
    ? `批量删除 (${deletableCount})`
    : "批量删除";
  deleteSelectedButton.title = activeCount
    ? `${activeCount} 个运行中或排队中的项目不会被删除`
    : "永久删除选中的视频项目及其本地文件";
  $("#selectAllTasks").textContent = tasks.length && state.selectedTasks.size === tasks.length
    ? "取消全选"
    : "选择全部";
  const list = $("#taskList");
  if (!tasks.length) {
    list.innerHTML = `
      <div class="empty-state">
        <span>00</span><h3>还没有下载任务</h3>
        <p>从上方搜索视频，或直接输入视频 ID 开始。</p>
      </div>`;
    return;
  }
  list.innerHTML = tasks.map((task) => {
    const selected = state.selectedTasks.has(task.task);
    const active = task.active_job;
    const progress = active ? active.progress : task.progress;
    const automationSkipped = !active && task.automation_skipped === true;
    const reviewSummary = !active ? (task.review_summary || "") : "";
    const summaryClass = automationSkipped
      ? "automation-skip-summary"
      : reviewSummary ? "review-summary" : "";
    const progressPercent = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
    const subtitle = active
      ? active.status === "queued" ? "队列中" : "执行中"
      : reviewSummary || (progressPercent >= 100 ? "已完成" : "等待继续");
    const author = task.channel || task.video_id || task.task;
    const subtitleStatus = task.chinese_auto_available
      ? `YouTube 自动中文可用 · ${task.chinese_auto_name}`
      : "没有 YouTube 自动中文字幕";
    const subtitleStatusLabel = task.chinese_auto_available
      ? `自动中文 · ${task.chinese_auto_name}`
      : "无自动中文字幕";
    const downloadWarnings = Array.isArray(task.download_warnings) ? task.download_warnings : [];
    const downloadWarning = downloadWarnings.length
      ? `<span class="task-warning" title="${escapeHtml(downloadWarnings.join("；"))}" aria-label="下载警告">⚠️</span>`
      : "";
    const rawStatusLabel = String(task.overall ?? "");
    const compactStatusLabels = {
      "翻译并检查中文字幕": "生成中文字幕",
      "生成中文 AI 配音": "生成中文配音",
      "生成并质检中文配音成片": "生成配音成片",
      "生成并质检双语成片": "生成双语成片",
      "生成无配音视频投稿信息": "投稿哔哩哔哩",
      "自动生成投稿标题、标签与分区": "投稿哔哩哔哩",
    };
    const statusLabel = compactStatusLabels[rawStatusLabel]
      || (rawStatusLabel === "正在投稿" || rawStatusLabel.startsWith("投稿解析")
        ? "投稿哔哩哔哩"
        : rawStatusLabel);
    const stages = Object.entries(task.stages).map(([key, value]) =>
      `<span class="stage ${escapeHtml(value.state)}" title="${escapeHtml(value.detail)}">${stageNames[key]}</span>`
    ).join("");
    const image = task.thumbnail_url
      ? `<img class="task-thumb" src="${escapeHtml(task.thumbnail_url)}" alt="" loading="lazy">`
      : `<div class="task-thumb"></div>`;
    const activePublishPriorityAction = active && active.status === "queued"
      ? `<button class="task-menu-item prioritize-active-task" type="button" role="menuitem" title="将此视频的排队投稿置顶" aria-label="优先投稿" data-job-id="${escapeHtml(active.id)}">优先投稿</button>`
      : active && active.kind === "publish"
        ? `<button class="task-menu-item" type="button" role="menuitem" disabled title="当前投稿正在上传，不能移动">优先投稿（当前上传中）</button>`
        : "";
    const publishAction = active ? activePublishPriorityAction : task.stages.publish.state === "complete"
      ? task.bilibili_url
        ? `<button class="task-menu-item open-bilibili" type="button" role="menuitem" title="打开B站稿件" aria-label="打开B站稿件">打开B站稿件</button>`
        : ""
      : task.stages.publish.state === "active"
        ? ""
      : task.stages.render.state === "complete"
          ? `<button class="task-menu-item publish-task" type="button" role="menuitem" title="投稿到哔哩哔哩" aria-label="投稿到哔哩哔哩">投稿到哔哩哔哩</button>
             <button class="task-menu-item publish-priority-task" type="button" role="menuitem" title="打开投稿窗口并置顶排队" aria-label="优先投稿">优先投稿</button>`
          : "";
    const publishPriorityAction = !active
      && task.stages.publish.state !== "complete"
      && task.stages.publish.state !== "active"
      ? task.stages.render.state === "complete"
        ? ""
        : `<button class="task-menu-item" type="button" role="menuitem" disabled title="请先完成成片或排版复核">优先投稿（需先完成成片）</button>`
      : "";
    const layoutReview = !active
      && task.stages.publish.state !== "complete"
      && !["ORIGINAL_MEDIA", "FALLBACK_PENDING"].includes(task.automation_status)
      && task.stage4_status === "REVIEW_REQUIRED"
      && task.review?.code === "SUBTITLE_LAYOUT_REVIEW_REQUIRED";
    const renderAction = layoutReview
      ? `<button class="task-menu-item review-task" type="button" role="menuitem" title="审核过长字幕并继续成片" aria-label="审核过长字幕并继续成片">审核字幕并继续成片</button>`
      : !active
        && task.stages.publish.state !== "complete"
        && !["ORIGINAL_MEDIA", "FALLBACK_PENDING"].includes(task.automation_status)
        && task.stages.translation.state === "complete"
        && task.stage4_status !== "STAGE4_COMPLETED"
        ? `<button class="task-menu-item render-task" type="button" role="menuitem" title="仅重新成片" aria-label="仅重新成片">仅重新成片</button>`
        : "";
    return `
      <article class="task-row ${selected ? "selected" : ""}" data-task="${escapeHtml(task.task)}" data-video-id="${escapeHtml(task.video_id)}" data-title="${escapeHtml(task.title)}" data-bilibili-url="${escapeHtml(task.bilibili_url || "")}">
        <div class="task-info">
          <input class="task-check" type="checkbox" aria-label="选择 ${escapeHtml(task.title)}" ${selected ? "checked" : ""}>
          ${image}
          <div class="task-title">
            <strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong>
            <div class="task-meta">
              <span class="task-author" title="${escapeHtml(author)}">${escapeHtml(author)}</span>
              <small class="task-subtitle-status ${task.chinese_auto_available ? "available" : "missing"}" title="${escapeHtml(subtitleStatus)}">${escapeHtml(subtitleStatusLabel)}</small>
            </div>
          </div>
        </div>
        <div class="task-workflow">
          <div class="status-cell">
            <div class="status-line">
              <strong class="${automationSkipped ? "automation-skip-status" : ""}" title="${escapeHtml(task.overall)}">${escapeHtml(statusLabel)}</strong>
              <span class="status-separator" aria-hidden="true">·</span>
              <small class="${summaryClass}" title="${escapeHtml(reviewSummary || subtitle)}">${escapeHtml(subtitle)}</small>
              ${downloadWarning}
              <span class="status-percent">${progressPercent}%</span>
            </div>
            <div class="status-progress">
              <progress class="progress-mini" max="100" value="${progressPercent}" aria-label="进度 ${progressPercent}%"></progress>
            </div>
          </div>
          <div class="stage-track">${stages}</div>
        </div>
        <div class="task-actions">
          ${active
            ? `<button class="icon-button danger cancel-task-job" type="button" data-job-id="${escapeHtml(active.id)}" title="终止当前进程" aria-label="终止当前进程">■</button>`
            : ""}
          <button class="icon-button open-folder" type="button" title="打开任务目录" aria-label="打开任务目录">↗</button>
          <button class="icon-button task-more" type="button" title="更多任务操作" aria-label="更多任务操作" aria-haspopup="menu" aria-expanded="false">…</button>
          <div class="task-more-menu" role="menu" hidden>
            ${publishAction}
            ${publishPriorityAction}
            ${renderAction}
            <button class="task-menu-item redownload-task" type="button" role="menuitem" title="${active ? "请先终止或等待当前任务完成" : "重新下载并修复原始素材"}" ${active ? "disabled" : ""}>重新下载</button>
            <button class="task-menu-item preview-cover" type="button" role="menuitem" title="预览原封面、中文封面或重新生成">封面与封面文案</button>
            ${task.dubbing_available
              ? '<button class="task-menu-item open-dubbing-folder" type="button" role="menuitem" title="打开中文配音目录">打开配音目录</button>'
              : ""}
            <button class="task-menu-item danger delete-task" type="button" role="menuitem" title="${active ? "请先终止运行中的任务" : "删除视频任务及全部文件"}" ${active ? "disabled" : ""}>删除任务</button>
          </div>
        </div>
      </article>`;
  }).join("");
}

function renderJobs(jobs) {
  const container = $("#jobList");
  if (!jobs.length) {
    container.innerHTML = '<p class="muted">暂无运行记录</p>';
    return;
  }
  container.innerHTML = jobs.slice(0, 20).map((job) => {
    const isActive = ["queued", "running"].includes(job.status);
    const retry = ["failed", "cancelled"].includes(job.status)
      ? `<button class="button button-ghost retry-job" type="button" data-job-id="${job.id}">重试</button>`
      : "";
    const cancel = isActive
      ? `<button class="button button-danger cancel-job" type="button" data-job-id="${job.id}">终止</button>`
      : "";
    const showLog = job.has_log || isActive
      ? `<button class="button button-ghost show-log" type="button" data-job-id="${job.id}" data-job-title="${escapeHtml(targetLabel(job.target))}">查看日志</button>`
      : "";
    const showResult = job.kind === "discovery" && job.status === "completed"
      ? `<button class="button button-ghost show-discovery-result" type="button" data-job-id="${job.id}">查看结果</button>`
      : "";
    const deleteLog = job.has_log && !isActive
      ? `<button class="button button-danger-outline delete-job-log" type="button" data-job-id="${job.id}">删日志</button>`
      : "";
    const kind = job.kind === "download" ? "DOWNLOAD"
      : job.kind === "cover" ? "COVER"
      : job.kind === "publish" ? "PUBLISH"
        : job.kind === "discovery" ? "DISCOVERY"
          : job.kind === "learning" ? "内容学习"
          : "PIPELINE";
    const resource = {
      network: "下载槽",
      gpu_heavy: job.kind === "discovery" ? "本地 AI 槽" : "本地重任务槽",
      paid_api: "AI API 槽",
      upload: "投稿槽",
    }[job.resource_class] || "";
    const runningHint = job.status === "running" && job.kind === "publish"
      ? ` · 已用时 ${elapsedText(job.started_at)} · biliup 不返回实时百分比`
      : "";
    const status = {
      queued: "等待中",
      running: "运行中",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
    }[job.status] || job.status;
    return `
      <article class="job-item">
        <div class="job-top"><span class="job-kind">${kind}${resource ? ` · ${resource}` : ""}</span><span class="job-status">${status} · ${job.progress}%</span></div>
        <strong title="${escapeHtml(job.target)}">${escapeHtml(targetLabel(job.target))}</strong>
        <p>${escapeHtml(job.step)}${escapeHtml(runningHint)}${job.error ? ` · ${escapeHtml(job.error)}` : ""}</p>
        <progress class="progress-mini" max="100" value="${job.progress}" aria-label="进度 ${job.progress}%"></progress>
        <div class="job-actions">
          ${showResult}
          ${showLog}
          ${cancel}
          ${retry}
          ${deleteLog}
        </div>
      </article>`;
  }).join("");
}

function visibleSearchResults() {
  if (!state.discoveryPayload) return state.searchResults;
  const summary = state.discoveryPayload.summary || {};
  const policyVersion = Number(summary.selection_policy_version || 0);
  const legacyQualityPolicy = policyVersion < 2;
  const backendUsesTieredFill = policyVersion >= 4;
  const requireHeatFloor = legacyQualityPolicy || summary.popularity_filter_mode === "hard";
  const excludeLlmRejects = legacyQualityPolicy || summary.exclude_llm_rejects !== false;
  const backendMinimumScore = Number(summary.minimum_opportunity_score ?? 50);
  const captionsOnly = $("#discoveryCaptionsOnly").checked;
  const hideSimilar = $("#discoveryHideSimilar").checked;
  const minScore = Number($("#discoveryMinScore").value || 0);
  return state.searchResults.filter((item) =>
    (backendUsesTieredFill || !requireHeatFloor || item.heat_floor_pass !== false)
    && (backendUsesTieredFill || !excludeLlmRejects || item.llm_status !== "scored" || item.llm_verdict !== "reject")
    && (backendUsesTieredFill || Number(item.opportunity_score || 0) >= backendMinimumScore)
    && (!captionsOnly || item.has_caption)
    && (!hideSimilar || !item.similar_candidate)
    && Number(item.opportunity_score || 0) >= minScore
  );
}

function searchResultCard(item) {
  const selected = state.selectedResults.has(item.video_id);
  const qualityTier = item.selection_tier === "reserve"
    ? " · 补量备选"
    : item.heat_tier === "expanded" ? " · 扩展优选" : " · 优选";
  const capacityTier = item.diversity_backfill ? " · 同频道补位" : "";
  const discoveryMeta = state.discoveryPayload ? `
    <div class="discovery-score-row">
      <strong>机会分 ${Number(item.opportunity_score || 0).toFixed(1)}</strong>
      <span>${item.llm_status === "scored" ? "Qwen 已评审" : "规则评分"}${qualityTier}${capacityTier} · 发布 ${discoveryAge(item.age_hours)}</span>
    </div>
    <p class="discovery-reason">${escapeHtml(item.selection_reason || "")}</p>
    <small class="collision-state ${item.similar_candidate ? "warning" : "safe"}">${escapeHtml(item.collision_status || "")}</small>
    <div class="discovery-feedback" aria-label="候选反馈">
      <button type="button" data-feedback="interested" title="希望以后多找类似视频">感兴趣</button>
      <button type="button" data-feedback="boring" title="内容无聊或太普通">无聊</button>
      <button type="button" data-feedback="irrelevant" title="与所选领域不相关">不相关</button>
      <button type="button" data-feedback="wrong_language" title="语言不符合要求">语言不对</button>
    </div>
  ` : "";
  return `
    <article class="result-card ${item.selection_tier === "reserve" ? "reserve" : ""} ${selected ? "selected" : ""}" data-video-id="${escapeHtml(item.video_id)}">
      <label class="result-select">
        <input class="result-check" type="checkbox" aria-label="选择 ${escapeHtml(item.title)}" ${selected ? "checked" : ""}>
      </label>
      <img src="${escapeHtml(item.thumbnail_url)}" alt="" loading="lazy">
      <div class="result-body">
        <h3 title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</h3>
        <p>${escapeHtml(item.channel_title)}</p>
        ${discoveryMeta}
        <div class="result-meta">
          <span>${escapeHtml(item.duration)}</span>
          <span>${number(item.view_count)} 次观看</span>
          <span>${item.has_caption ? "有字幕" : "无字幕"}</span>
        </div>
      </div>
    </article>`;
}

function summarizeDiscoveryWarnings(rawWarnings, searchQuotaExhausted = false) {
  const warningItems = [];
  const detailedAiWarnings = [];
  const seenWarnings = new Set();
  const failedCandidateIds = new Set();
  for (const rawMessage of rawWarnings) {
    const message = String(rawMessage || "").trim();
    if (!message || (searchQuotaExhausted && message.includes("配额"))) continue;
    const candidateFailure = message.match(/^本地 AI 无法评审\s+([^：:]+)[：:]\s*(.+)$/);
    if (candidateFailure) {
      failedCandidateIds.add(candidateFailure[1].trim());
      if (!seenWarnings.has(message)) detailedAiWarnings.push(message);
      seenWarnings.add(message);
      continue;
    }
    if (!seenWarnings.has(message)) warningItems.push(message);
    seenWarnings.add(message);
  }
  if (detailedAiWarnings.length) {
    const failedCount = failedCandidateIds.size || detailedAiWarnings.length;
    warningItems.push(
      `本地 AI 有 ${failedCount} 个候选未完成评审，已自动回退到规则评分。请确认 Ollama 服务和模型保持可用。`,
    );
  }
  return { warningItems, detailedAiWarnings };
}

function discoveryQueryDiagnosticsMarkup(diagnostics) {
  if (!Array.isArray(diagnostics) || !diagnostics.length) return "";
  if (diagnostics[0].run_id) {
    return `<details class="discovery-query-diagnostics"><summary>动态查询绩效 · ${diagnostics.length} 次执行</summary>
      <p>来源仅用于归因，领域由视频内容判定。同一视频可能命中多个查询，各行不可相加。</p>
      <div class="discovery-query-table-wrap"><table><thead><tr><th>查询 / 实体 / 形式</th><th>排序</th><th>返回</th><th>新增</th><th>合格</th><th>AI 高质量</th><th>展示</th><th>状态</th></tr></thead><tbody>
      ${diagnostics.map((r) => `<tr><td>${escapeHtml(r.query)}<br><small>${escapeHtml(r.entity)} / ${escapeHtml(r.format)}</small></td><td>${escapeHtml(r.order)}</td>${[r.returned_count, r.new_unique_count, r.eligible_count, r.ai_high_quality_count, r.shown_count].map((n) => `<td>${Number(n || 0)}</td>`).join("")}<td>${escapeHtml(r.status)}</td></tr>`).join("")}
      </tbody></table></div></details>`;
  }
  const orderLabels = { relevance: "相关性", viewCount: "热门", date: "最新" };
  const count = (value) => Math.max(0, Number(value) || 0);
  return `<details class="discovery-query-diagnostics">
    <summary>搜索词效果 · 本轮执行 ${diagnostics.length} 个词</summary>
    <p>新增合格：通过规则且未出现在保留的历史结果或已处理任务中。频道数统计规则保留的视频；同一视频可命中多个词，各行不可相加。</p>
    <div class="discovery-query-table-wrap"><table>
      <thead><tr><th>搜索词</th><th>排序</th><th>调用</th><th>去重召回</th><th>规则保留</th><th>新增合格</th><th>频道</th><th>入选</th></tr></thead>
      <tbody>${diagnostics.map((item) => `<tr>
        <td>${escapeHtml(item.query || "")}</td>
        <td>${escapeHtml((Array.isArray(item.orders) ? item.orders : []).map((order) => orderLabels[order] || order).join(" / "))}</td>
        <td>${count(item.calls)}</td><td>${count(item.unique_count)}</td>
        <td>${count(item.eligible_count)}</td><td>${count(item.new_eligible_count)}</td>
        <td>${count(item.channel_count)}</td><td>${count(item.selected_count)}</td>
      </tr>`).join("")}</tbody>
    </table></div>
  </details>`;
}

function renderSearchResults() {
  const section = $("#searchResultsSection");
  const isDiscovery = Boolean(state.discoveryPayload);
  section.classList.toggle("hidden", !isDiscovery && !state.searchResults.length);
  const visible = visibleSearchResults();
  const visibleIds = new Set(visible.map((item) => item.video_id));
  $("#searchResultsTitle").textContent = isDiscovery
    ? `智能发现候选 · 显示 ${visible.length} / ${state.searchResults.length} 个领域候选位`
    : `搜索结果 · ${state.searchResults.length}`;
  $("#discoveryResultFilters").classList.toggle("hidden", !isDiscovery);
  const container = $("#searchResults");
  container.className = isDiscovery ? "result-groups" : "result-grid";
  const warningBox = $("#discoveryWarnings");
  if (!isDiscovery) {
    warningBox.classList.add("hidden");
    warningBox.innerHTML = "";
    container.innerHTML = visible.map(searchResultCard).join("");
    return;
  }
  const summary = state.discoveryPayload.summary || {};
  const groups = Array.isArray(state.discoveryPayload.groups) ? state.discoveryPayload.groups : [];
  const isNext = summary.recall_architecture === "discovery_next";
  const limitPerPack = Number(summary.result_limit_per_pack || summary.result_target_per_pack || state.discoveryPayload.per_pack || 0);
  const recalledByPack = summary.recalled_counts_by_pack || {};
  const recalledAssignmentCount = Object.values(recalledByPack)
    .reduce((total, value) => total + Number(value || 0), 0);
  const uniqueResultCount = Number(summary.unique_result_count ?? summary.result_count ?? 0);
  const assignmentCount = Number(summary.result_count ?? state.searchResults.length);
  const qualityEligibleCount = Number(summary.preferred_eligible_count ?? summary.selection_eligible_count ?? visible.length);
  const expandedResultCount = Number(summary.expanded_result_count || 0);
  const reserveResultCount = Number(summary.reserve_result_count || 0);
  const diversityBackfillCount = Number(summary.diversity_backfill_result_count || 0);
  const uniqueResultNote = uniqueResultCount < assignmentCount
    ? `（${uniqueResultCount} 个不重复视频）`
    : "";
  $("#discoveryResultSummary").textContent =
    `${summary.selected_pack_count || 0} 个领域 · 每领域目标 ${limitPerPack} 条 · `
    + `时长 ${Math.round(Number(summary.minimum_duration_seconds || 0) / 60)}–${Math.round(Number(summary.maximum_duration_seconds || 0) / 60)} 分钟 · `
    + `各领域召回 ${recalledAssignmentCount}/${summary.recall_target || 0} 条、去重 ${summary.raw_candidate_count || 0} 条（搜索 ${summary.search_request_count || 0}/${summary.search_request_limit || 0} 次） → `
    + `规则保留 ${summary.eligible_count || 0} 条 → AI ${summary.llm_scored_count || 0}/${summary.llm_candidate_count || 0} 条 → 优选 ${qualityEligibleCount} 条 → `
    + `最终 ${assignmentCount} 个领域候选位${uniqueResultNote}（扩展优选 ${expandedResultCount} 条、补量备选 ${reserveResultCount} 条、同频道补位 ${diversityBackfillCount} 条）、当前显示 ${visible.length} 条 · 视觉复评 ${summary.visual_scored_count || 0} 条`;
  if (isNext) {
    $("#discoveryResultSummary").textContent = `Discovery Next · 学习样本 ${summary.sample_size || 0} · 搜索 ${summary.search_request_count}/${summary.search_request_limit} 次 · 去重召回 ${summary.raw_candidate_count} · 合格 ${summary.eligible_count} · AI 分析 ${summary.llm_scored_count} · 展示 ${assignmentCount} 条（当前可见 ${visible.length}）`;
  }

  const resultCounts = summary.result_counts_by_pack || {};
  const rawWarnings = Array.isArray(summary.warnings) ? summary.warnings : [];
  const warningSummary = summarizeDiscoveryWarnings(
    rawWarnings,
    Boolean(summary.search_quota_exhausted),
  );
  const warningItems = [...warningSummary.warningItems];
  if (summary.search_quota_exhausted) {
    warningItems.unshift("YouTube 今日搜索配额已耗尽，本次只能筛选配额耗尽前召回的视频。请在配额每日重置后重试，或在 Google Cloud 申请更多 Search Queries 配额。");
  }
  const zeroRecallLabels = groups
    .filter((group) => Number(recalledByPack[group.id] || 0) === 0)
    .map((group) => group.label);
  if (zeroRecallLabels.length) {
    warningItems.push(`以下领域本轮没有匹配内容：${zeroRecallLabels.join("、")}。可调整领域描述、实体和内容形式，或扩大时间范围；搜索预算按动态策略分配。`);
  }
  const durationExcluded = Number(summary.excluded?.duration || 0);
  if (durationExcluded) {
    warningItems.push(`${durationExcluded} 条视频因不满足本次时长范围被排除。`);
  }
  warningBox.classList.toggle("hidden", !warningItems.length);
  const detailedWarnings = warningSummary.detailedAiWarnings;
  const detailMarkup = detailedWarnings.length
    ? `<details class="discovery-warning-details"><summary>查看 ${detailedWarnings.length} 条逐视频错误详情</summary><ul>${detailedWarnings.map((message) => `<li>${escapeHtml(message)}</li>`).join("")}</ul></details>`
    : "";
  warningBox.innerHTML = warningItems.length
    ? `<strong>智能发现提示</strong><ul>${warningItems.map((message) => `<li>${escapeHtml(message)}</li>`).join("")}</ul>${detailMarkup}`
    : "";

  container.innerHTML = groups.map((group) => {
    const allRows = Array.isArray(group.results) ? group.results : [];
    const actualCount = Number(resultCounts[group.id] ?? allRows.length);
    const rows = allRows.filter((item) => visibleIds.has(item.video_id));
    const eligibleCount = Number(summary.preferred_eligible_counts_by_pack?.[group.id] ?? rows.length);
    const reserveCount = allRows.filter((item) => item.selection_tier === "reserve").length;
    const emptyMessage = actualCount
      ? "本领域已有候选，但都被当前的字幕、重复或最低分筛选隐藏。"
      : Number(recalledByPack[group.id] || 0) === 0
        ? "本轮没有内容匹配此领域的候选。"
        : `本领域没有通过硬性安全条件的候选；优选 ${eligibleCount} 条。`;
    return `
      <section class="result-group" data-pack-id="${escapeHtml(group.id)}">
        <div class="result-group-heading">
          <div><h3>${escapeHtml(group.label)}</h3><p>${escapeHtml(group.description)} · 候选 ${actualCount}/目标 ${limitPerPack}（补量备选 ${reserveCount}） · 当前显示 ${rows.length} 条</p></div>
          ${rows.length ? '<button class="button button-ghost button-small select-discovery-group" type="button">选择本区</button>' : ""}
        </div>
        ${discoveryQueryDiagnosticsMarkup((Array.isArray(summary.query_diagnostics) ? summary.query_diagnostics : []).filter((item) => item.pack_id === group.id))}
        ${rows.length
    ? `<div class="result-grid">${rows.map(searchResultCard).join("")}</div>`
    : `<div class="result-group-empty">${escapeHtml(emptyMessage)}</div>`}
      </section>`;
  }).join("") || '<div class="empty-state"><span>00</span><h3>没有发现领域</h3><p>请重新选择至少一个领域后运行。</p></div>';
}

function applyDiscoveryResult(payload) {
  if (!payload || typeof payload !== "object") {
    throw new Error("智能发现结果格式无效");
  }
  state.discoveryPayload = payload;
  state.searchResults = Array.isArray(payload.results) ? payload.results : [];
  state.selectedResults = new Set(
    state.searchResults.filter((item) => !item.similar_candidate).map((item) => item.video_id),
  );
  renderSearchResults();
}

async function loadDiscoveryResult(jobId, { scroll = true, announce = true } = {}) {
  const response = await api(`/api/discovery/result?job_id=${encodeURIComponent(jobId)}`);
  const job = response.job || {};
  if (job.status !== "completed") {
    throw new Error(job.error || "智能发现任务尚未完成");
  }
  if (!response.result) {
    throw new Error("智能发现完成但没有结果文件");
  }
  applyDiscoveryResult(response.result);
  if (scroll) {
    $("#searchResultsSection").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (announce) {
    toast(`已载入 ${state.searchResults.length} 个智能候选`);
  }
  return response.result;
}

async function restoreLatestDiscoveryResult(jobs) {
  if (!state.discoveryAutoRestorePending || state.discoveryJobId
      || state.discoveryPayload || state.searchResults.length) return;
  const discoveryJobs = jobs.filter((job) => job.kind === "discovery");
  if (!discoveryJobs.length) {
    state.discoveryAutoRestorePending = false;
    return;
  }
  const latest = discoveryJobs[0];
  if (["queued", "running"].includes(latest.status)) {
    updateDiscoveryProgress(latest);
    return;
  }
  state.discoveryAutoRestorePending = false;
  const completed = discoveryJobs.find((job) => job.status === "completed");
  if (!completed) return;
  try {
    await loadDiscoveryResult(completed.id, { scroll: false, announce: false });
  } catch (error) {
    console.warn("无法自动恢复最近的智能发现结果", error);
  }
}

function renderDiscoveryPacks() {
  $("#discoveryPacks").innerHTML = state.discoveryCatalog.map((pack) => `
    <label class="discovery-pack">
      <input type="checkbox" value="${escapeHtml(pack.id)}" ${pack.default_selected ? "checked" : ""}>
      <span><strong>${escapeHtml(pack.label)}</strong><small>${escapeHtml(pack.description)}</small></span>
    </label>
  `).join("");
}

async function loadDiscoveryCatalog() {
  try {
    const payload = await api("/api/discovery/packs");
    state.discoveryCatalog = payload.packs || [];
    renderDiscoveryPacks();
  } catch (error) {
    $("#discoveryPacks").innerHTML = `<span class="muted">${escapeHtml(error.message)}</span>`;
  }
}

function setAllDiscoveryPacks(checked) {
  $$("#discoveryPacks input[type=checkbox]").forEach((input) => { input.checked = checked; });
}

$("#selectAllDiscoveryPacks").addEventListener("click", () => setAllDiscoveryPacks(true));
$("#clearDiscoveryPacks").addEventListener("click", () => setAllDiscoveryPacks(false));

function updateDiscoveryProgress(job) {
  $("#discoveryJobProgress").classList.remove("hidden");
  $("#discoveryJobStep").textContent = job.step || "正在智能发现";
  $("#discoveryJobProgressBar").value = Number(job.progress || 0);
}

async function waitForDiscovery(jobId) {
  while (state.discoveryJobId === jobId) {
    const payload = await api(`/api/discovery/result?job_id=${encodeURIComponent(jobId)}`);
    const job = payload.job || {};
    updateDiscoveryProgress(job);
    if (job.status === "completed") return payload.result;
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(job.error || (job.status === "cancelled" ? "智能发现已终止" : "智能发现失败"));
    }
    await delay(900);
  }
  throw new Error("智能发现任务已被新的请求替换");
}

$("#discoveryForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.discoveryAutoRestorePending = false;
  const packs = $$("#discoveryPacks input:checked").map((input) => input.value);
  const discoveryScope = $("#discoveryScope").value;
  if (discoveryScope === "manual" && !packs.length) return toast("手动聚焦请至少选择一个领域", true);
  const button = $(".discovery-submit", event.currentTarget);
  button.disabled = true;
  button.querySelector("span").textContent = "正在发现…";
  try {
    const queued = await api("/api/discover", {
      method: "POST",
      body: JSON.stringify({
        packs,
        hours: Number($("#discoveryHours").value),
        per_pack: Number($("#discoveryPerPack").value),
        minimum_duration_minutes: Number($("#discoveryMinDurationMinutes").value),
        maximum_duration_minutes: Number($("#discoveryMaxDurationMinutes").value),
        discovery_scope: discoveryScope,
        search_strength: $("#discoverySearchStrength").value,
      }),
    });
    const jobId = queued.job?.id;
    if (!jobId) throw new Error("智能发现任务没有返回 ID");
    state.discoveryJobId = jobId;
    updateDiscoveryProgress(queued.job);
    const payload = await waitForDiscovery(jobId);
    if (!payload) throw new Error("智能发现完成但没有结果文件");
    applyDiscoveryResult(payload);
    $("#searchResultsSection").scrollIntoView({ behavior: "smooth", block: "start" });
    toast(`筛选出 ${state.searchResults.length} 个智能候选`);
    const warnings = payload.summary?.warnings || [];
    const warningSummary = summarizeDiscoveryWarnings(
      warnings,
      Boolean(payload.summary?.search_quota_exhausted),
    );
    if (warningSummary.warningItems.length) {
      setTimeout(() => toast(warningSummary.warningItems[0], true), 500);
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.discoveryJobId = null;
    button.disabled = false;
    button.querySelector("span").textContent = "智能筛选有趣视频";
  }
});

for (const id of ["discoveryCaptionsOnly", "discoveryHideSimilar", "discoveryMinScore"]) {
  $("#" + id).addEventListener("change", renderSearchResults);
}

$$(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    $$(".tab").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === button.dataset.tab));
  });
});

$("#searchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.discoveryAutoRestorePending = false;
  const button = $(".search-submit", event.currentTarget);
  button.disabled = true;
  button.querySelector("span").textContent = "搜索中…";
  try {
    const payload = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        query: $("#searchQuery").value,
        limit: Number($("#searchLimit").value),
        order: $("#searchOrder").value,
      }),
    });
    state.discoveryPayload = null;
    state.searchResults = payload.results;
    state.selectedResults = new Set(payload.results.map((item) => item.video_id));
    renderSearchResults();
    $("#searchResultsSection").scrollIntoView({ behavior: "smooth", block: "start" });
    toast(`找到 ${payload.results.length} 个视频`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "开始搜索";
  }
});

$("#directForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("button[type=submit]", event.currentTarget);
  button.disabled = true;
  try {
    const payload = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({
        input: $("#directInput").value,
        confirm_rights: $("#directRights").checked,
        ...downloadRequestValues(),
      }),
    });
    $("#directInput").value = "";
    toast(`${payload.jobs.length} 个视频已加入下载队列`);
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#searchResults").addEventListener("change", (event) => {
  if (!event.target.classList.contains("result-check")) return;
  const card = event.target.closest(".result-card");
  if (event.target.checked) state.selectedResults.add(card.dataset.videoId);
  else state.selectedResults.delete(card.dataset.videoId);
  renderSearchResults();
});

$("#searchResults").addEventListener("click", (event) => {
  const feedbackButton = event.target.closest("[data-feedback]");
  if (feedbackButton) {
    const card = feedbackButton.closest(".result-card");
    const item = state.searchResults.find((row) => row.video_id === card?.dataset.videoId);
    if (!item) return;
    feedbackButton.disabled = true;
    api("/api/discovery/feedback", {
      method: "POST",
      body: JSON.stringify({ item, feedback: feedbackButton.dataset.feedback }),
    }).then(() => {
      $$("[data-feedback]", card).forEach((button) => {
        button.classList.remove("active");
        button.disabled = false;
      });
      feedbackButton.classList.add("active");
      toast("偏好已记录，将用于后续智能发现");
    }).catch((error) => {
      feedbackButton.disabled = false;
      toast(error.message, true);
    });
    return;
  }
  const button = event.target.closest(".select-discovery-group");
  if (!button) return;
  const group = button.closest(".result-group");
  const ids = $$(".result-card", group).map((card) => card.dataset.videoId);
  const allSelected = ids.every((videoId) => state.selectedResults.has(videoId));
  ids.forEach((videoId) => {
    if (allSelected) state.selectedResults.delete(videoId);
    else state.selectedResults.add(videoId);
  });
  renderSearchResults();
});

$("#selectAllResults").addEventListener("click", () => {
  const visible = visibleSearchResults();
  const allSelected = visible.length && visible.every((item) => state.selectedResults.has(item.video_id));
  visible.forEach((item) => {
    if (allSelected) state.selectedResults.delete(item.video_id);
    else state.selectedResults.add(item.video_id);
  });
  renderSearchResults();
});

$("#downloadResults").addEventListener("click", async () => {
  const visibleIds = new Set(visibleSearchResults().map((item) => item.video_id));
  const selectedItems = state.searchResults.filter(
    (item) => visibleIds.has(item.video_id) && state.selectedResults.has(item.video_id),
  );
  const items = [...new Map(selectedItems.map((item) => [item.video_id, item])).values()];
  if (!items.length) return toast("请先选择要下载的视频", true);
  const button = $("#downloadResults");
  button.disabled = true;
  try {
    const payload = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({
        items,
        confirm_rights: $("#searchRights").checked,
        ...downloadRequestValues(),
      }),
    });
    toast(`${payload.jobs.length} 个视频已加入下载队列`);
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#taskList").addEventListener("change", (event) => {
  if (!event.target.classList.contains("task-check")) return;
  const row = event.target.closest(".task-row");
  if (event.target.checked) state.selectedTasks.add(row.dataset.task);
  else state.selectedTasks.delete(row.dataset.task);
  renderTasks(state.dashboard?.tasks || []);
});

function closeTaskMenus() {
  $$(".task-more-menu").forEach((menu) => {
    menu.hidden = true;
    menu.closest(".task-row")?.classList.remove("menu-open");
    menu.closest(".task-actions")?.querySelector(".task-more")?.setAttribute("aria-expanded", "false");
  });
}

document.addEventListener("click", (event) => {
  if (!event.target.closest(".task-more, .task-more-menu")) closeTaskMenus();
});

$("#taskList").addEventListener("click", async (event) => {
  const row = event.target.closest(".task-row");
  if (!row) return;
  const moreButton = event.target.closest(".task-more");
  if (moreButton) {
    const menu = $(".task-more-menu", row);
    const shouldOpen = menu.hidden;
    closeTaskMenus();
    menu.hidden = !shouldOpen;
    row.classList.toggle("menu-open", shouldOpen);
    moreButton.setAttribute("aria-expanded", String(shouldOpen));
    event.stopPropagation();
    return;
  }
  if (event.target.closest(".task-more-menu")) closeTaskMenus();
  const redownloadButton = event.target.closest(".redownload-task");
  if (redownloadButton) {
    await queueRedownloads([row.dataset.task], redownloadButton);
    return;
  }
  if (event.target.closest(".preview-cover")) {
    state.coverPreviewTask = row.dataset.task;
    $("#coverRegeneratePaid").checked = false;
    $("#coverRegenerateCloud").checked = false;
    updateCoverPreview();
    $("#coverPreviewDialog").showModal();
    return;
  }
  const cancelButton = event.target.closest(".cancel-task-job");
  if (cancelButton) {
    if (!window.confirm("确定终止这个任务的当前进程吗？\n已生成的文件会保留，稍后仍可重试。")) return;
    cancelButton.disabled = true;
    try {
      await api(`/api/jobs/${cancelButton.dataset.jobId}/cancel`, {
        method: "POST",
        body: "{}",
      });
      toast("已发送终止请求");
      await refreshDashboard();
    } catch (error) {
      cancelButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
  const deleteButton = event.target.closest(".delete-task");
  if (deleteButton) {
    const label = row.dataset.title || row.dataset.videoId || targetLabel(row.dataset.task);
    const videoId = row.dataset.videoId || "";
    if (!window.confirm(`永久删除“${label}”及其全部本地文件？\n关联的作业记录和日志也会删除，此操作无法撤销。`)) return;
    const typed = window.prompt(`为防止误删，请输入视频号：${videoId}`);
    if (typed === null) return;
    if (typed.trim() !== videoId) return toast("视频号不匹配，已取消删除", true);
    deleteButton.disabled = true;
    try {
      const payload = await api("/api/tasks/delete", {
        method: "POST",
        body: JSON.stringify({
          task: row.dataset.task,
          confirmation: row.dataset.task,
        }),
      });
      state.selectedTasks.delete(row.dataset.task);
      toast(`已删除 ${payload.files} 个文件，共 ${formatBytes(payload.bytes)}`);
      await refreshDashboard();
    } catch (error) {
      deleteButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
  if (event.target.closest(".open-folder")) {
    try {
      await api("/api/open-folder", {
        method: "POST",
        body: JSON.stringify({ task: row.dataset.task }),
      });
    } catch (error) {
      toast(error.message, true);
    }
  }
  if (event.target.closest(".open-dubbing-folder")) {
    try {
      await api("/api/open-folder", {
        method: "POST",
        body: JSON.stringify({ task: row.dataset.task, subfolder: "dubbing" }),
      });
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  if (event.target.closest(".review-task")) {
    await openRenderReview(row.dataset.task);
    return;
  }
  if (event.target.closest(".render-task")) {
    state.selectedTasks = new Set([row.dataset.task]);
    await queueWorkflow("render");
  }
  if (event.target.closest(".publish-task")) {
    await openPublishDialog(row.dataset.task);
    return;
  }
  if (event.target.closest(".publish-priority-task")) {
    state.publishPriorityDefault = true;
    await openPublishDialog(row.dataset.task);
    return;
  }
  const activePriorityButton = event.target.closest(".prioritize-active-task");
  if (activePriorityButton) {
    await prioritizePublishJob(activePriorityButton);
    return;
  }
  if (event.target.closest(".open-bilibili") && row.dataset.bilibiliUrl) {
    window.open(row.dataset.bilibiliUrl, "_blank", "noopener,noreferrer");
  }
});

async function updateCoverPreview() {
  const task = (state.dashboard?.tasks || []).find((item) => item.task === state.coverPreviewTask);
  if (!task) return;
  $("#coverPreviewTitle").textContent = task.title;
  const cloudMode = state.dashboard?.health?.cover?.mode === "cloud";
  $("#coverPreviewMode").textContent = `本次重新生成模式：${cloudMode ? "当前翻译 API 生成文案 + Pillow 绘制大号中文" : "本地 Ollama + Pillow"}`;
  $("#coverRegenerateCloud").disabled = !cloudMode;
  $("#coverRegeneratePaid").disabled = cloudMode;
  if (!state.dashboard?.health?.cover) {
    for (const selector of ["#coverOriginalPreview", "#coverLocalizedPreview"]) {
      $(selector).classList.add("hidden");
      $(selector).removeAttribute("src");
    }
    $("#coverOriginalHint").textContent = "旧版后台不支持封面预览，不能据此判断原图缺失。";
    $("#coverMissingHint").classList.remove("hidden");
    $("#coverMissingHint").textContent = "请在任务完成后重启控制面板。";
    $("#coverPreviewStatus").textContent = "需重启后台：等待活动任务完成，关闭旧面板并重新运行 start_panel.bat，再刷新网页。";
    $("#coverPreviewCopy").textContent = "";
    $("#coverPreviewCandidates").textContent = "";
    $("#coverPreviewLog").textContent = "旧版后台不支持封面日志接口。";
    $("#regenerateCover").disabled = true;
    return;
  }
  $("#coverMissingHint").textContent = "尚未生成中文封面";
  $("#coverOriginalHint").textContent = task.cover_original_available ? "" : "任务目录中尚无原始封面。";
  for (const [selector, variant, available] of [
    ["#coverOriginalPreview", "original", task.cover_original_available],
    ["#coverLocalizedPreview", "localized", task.cover_localized_available],
  ]) {
    const img = $(selector);
    img.onerror = () => {
      img.classList.add("hidden");
      const hint = $(variant === "original" ? "#coverOriginalHint" : "#coverMissingHint");
      hint.classList.remove("hidden");
      hint.textContent = "封面加载失败，请刷新页面；若刚更新程序，请在任务完成后重启后台。";
    };
    img.classList.toggle("hidden", !available);
    if (available) {
      const url = `/api/cover?task=${encodeURIComponent(task.task)}&variant=${variant}&v=${encodeURIComponent(task.updated_at)}`;
      if (img.getAttribute("src") !== url) img.src = url;
    } else img.removeAttribute("src");
  }
  $("#coverMissingHint").classList.toggle("hidden", Boolean(task.cover_localized_available));
  $("#coverPreviewCopy").textContent = task.cover_copy ? `最终文案：${task.cover_copy}` : "尚无最终文案";
  $("#coverPreviewCandidates").textContent = `候选文案：${(task.cover_candidates || []).join(" / ") || "暂无"}`;
  $("#coverPreviewCandidates").textContent += (task.cover_warnings || []).length
    ? `\n注意：${task.cover_warnings.join("；")}` : "";
  $("#coverPreviewStatus").textContent = task.active_job
    ? `当前任务正在执行：${task.active_job.step || "处理中"}；完成后可重新生成封面。`
    : task.stages.cover?.detail || "尚未生成";
  $("#regenerateCover").disabled = Boolean(task.active_job);
  try {
    const details = await api(`/api/cover/details?task=${encodeURIComponent(task.task)}`);
    if (state.coverPreviewTask === task.task) $("#coverPreviewLog").textContent = details.log || "尚无日志";
  } catch (error) {
    if (state.coverPreviewTask === task.task) $("#coverPreviewLog").textContent = error.message;
  }
}

$("#closeCoverPreview").addEventListener("click", () => $("#coverPreviewDialog").close());
$("#coverPreviewDialog").addEventListener("close", () => { state.coverPreviewTask = null; });
$("#regenerateCover").addEventListener("click", async () => {
  if (!state.coverPreviewTask) return;
  const cloudMode = state.dashboard?.health?.cover?.mode === "cloud";
  if (cloudMode && !$("#coverRegenerateCloud").checked) {
    toast("请先勾选本次封面文案 API 授权", true);
    return;
  }
  $("#regenerateCover").disabled = true;
  try {
    await api("/api/cover", { method: "POST", body: JSON.stringify({
      task: state.coverPreviewTask, force: true,
      allow_paid_api: $("#coverRegeneratePaid").checked,
      allow_cloud_api: cloudMode && $("#coverRegenerateCloud").checked,
    }) });
    toast("中文封面已加入生成队列");
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
    $("#regenerateCover").disabled = false;
  }
});

function renderReviewRows(review) {
  const rows = review.rows || [];
  const canHideFromRender = review.supports_hide_from_render === true;
  $("#renderReviewStatus").textContent = review.ready_to_render
    ? review.hidden_count
      ? `排版预检已通过；成片将隐藏 ${review.hidden_count} 条经你确认忽略的字幕。`
      : "字幕修改已通过排版预检，可以继续成片。"
    : review.remaining_issue_count
      ? `仍有 ${review.remaining_issue_count} 条字幕未通过，请继续缩短后再检查。`
      : review.message;
  $("#renderReviewRows").innerHTML = rows.map((row) => `
    <article class="review-cue ${row.hidden_from_render ? "hidden-from-render" : ""}" data-cue-id="${escapeHtml(row.id)}">
      <div class="review-cue-head">
        <strong>字幕 ${escapeHtml(row.id)}</strong>
        <span>${escapeHtml(row.timecode)} · ${Number(row.duration).toFixed(2)} 秒</span>
      </div>
      <div class="review-issue-tags">
        ${(row.issue_labels || []).map((label) => `<span>${escapeHtml(label)}</span>`).join("")}
      </div>
      ${canHideFromRender ? `<label class="review-hide-option">
        <input class="review-hide" type="checkbox" ${row.hidden_from_render ? "checked" : ""}>
        <span><strong>忽略此条并继续生成</strong><small>仅在成片中隐藏这条中英文字幕；源字幕文件保持不变。</small></span>
      </label>` : `<div class="review-backend-update">当前后台仍是旧版本，隐藏选项暂不可用。请等待运行中的任务完成，然后重新启动控制面板。</div>`}
      <div class="review-originals">
        <p><b>原英文</b>${escapeHtml(row.english_original)}</p>
        <p><b>原中文</b>${escapeHtml(row.chinese_original)}</p>
      </div>
      <div class="review-edit-grid">
        <label class="field">
          <span>成片显示英文</span>
          <textarea class="review-english" rows="3" maxlength="2000" ${row.hidden_from_render ? "disabled" : "required"}>${escapeHtml(row.english_text)}</textarea>
        </label>
        <label class="field">
          <span>成片显示中文</span>
          <textarea class="review-chinese" rows="3" maxlength="2000" ${row.hidden_from_render ? "disabled" : "required"}>${escapeHtml(row.chinese_text)}</textarea>
        </label>
      </div>
    </article>
  `).join("");
}

$("#renderReviewRows").addEventListener("change", (event) => {
  const toggle = event.target.closest(".review-hide");
  if (!toggle) return;
  const row = toggle.closest(".review-cue");
  row.classList.toggle("hidden-from-render", toggle.checked);
  $$("textarea", row).forEach((textarea) => {
    textarea.disabled = toggle.checked;
    textarea.required = !toggle.checked;
  });
  const pendingHidden = $$(".review-hide:checked", $("#renderReviewRows")).length;
  $("#renderReviewStatus").textContent = pendingHidden
    ? `已选择在成片中隐藏 ${pendingHidden} 条字幕，但尚未保存；请点击底部“保存、重新检查并继续成片”。`
    : "审核选项已更改但尚未保存；请点击底部“保存、重新检查并继续成片”。";
});

async function openRenderReview(task) {
  state.renderReviewTask = task;
  $("#renderReviewTitle").textContent = targetLabel(task);
  $("#renderReviewStatus").textContent = "正在读取需要复核的字幕…";
  $("#renderReviewRows").innerHTML = "";
  $("#submitRenderReview").disabled = true;
  $("#renderReviewDialog").showModal();
  try {
    const payload = await api(`/api/render-review?task=${encodeURIComponent(task)}`);
    $("#renderReviewMode").value = ["ass", "softsub", "hardsub", "both"].includes(payload.output_mode)
      ? payload.output_mode
      : "hardsub";
    renderReviewRows(payload);
    $("#submitRenderReview").disabled = false;
  } catch (error) {
    $("#renderReviewStatus").textContent = error.message;
    toast(error.message, true);
  }
}

function closeRenderReview() {
  $("#renderReviewDialog").close();
  state.renderReviewTask = null;
}

$("#closeRenderReview").addEventListener("click", closeRenderReview);
$("#cancelRenderReview").addEventListener("click", closeRenderReview);
$("#renderReviewDialog").addEventListener("close", () => { state.renderReviewTask = null; });
$("#renderReviewForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.renderReviewTask) return;
  const button = $("#submitRenderReview");
  button.disabled = true;
  const edits = $$(".review-cue", $("#renderReviewRows")).map((row) => ({
    id: row.dataset.cueId,
    english: $(".review-english", row).value,
    chinese: $(".review-chinese", row).value,
    hidden_from_render: $(".review-hide", row)?.checked === true,
  }));
  try {
    const payload = await api("/api/render-review", {
      method: "POST",
      body: JSON.stringify({
        task: state.renderReviewTask,
        render_mode: $("#renderReviewMode").value,
        edits,
      }),
    });
    if (payload.job) {
      toast("排版复核通过，成片任务已加入队列");
      closeRenderReview();
      await refreshDashboard();
      return;
    }
    renderReviewRows(payload.review);
    toast(`仍有 ${payload.review.remaining_issue_count} 条字幕不满足单行和显示时长要求`, true);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#selectAllTasks").addEventListener("click", () => {
  const tasks = state.dashboard?.tasks || [];
  const allSelected = tasks.length && state.selectedTasks.size === tasks.length;
  state.selectedTasks = allSelected ? new Set() : new Set(tasks.map((task) => task.task));
  renderTasks(tasks);
});

$("#redownloadSelectedTasks").addEventListener("click", async () => {
  const tasks = (state.dashboard?.tasks || [])
    .filter((task) => state.selectedTasks.has(task.task) && !task.active_job)
    .map((task) => task.task);
  await queueRedownloads(tasks, $("#redownloadSelectedTasks"));
});

async function queueRedownloads(tasks, button) {
  if (!tasks.length) return toast("请先选择没有运行或排队作业的视频任务", true);
  if (tasks.length > 50) return toast("一次最多重新下载 50 个视频", true);
  if (!window.confirm(
    `重新下载这 ${tasks.length} 个项目的原始素材？\n\n新素材校验通过后放回原目录，旧素材会备份。已有翻译、审核字幕和成片会保留；完成后可手动继续处理。\n\n继续即确认你拥有下载和使用这些视频的权利。`,
  )) return;
  button.disabled = true;
  try {
    const payload = await api("/api/tasks/redownload", {
      method: "POST",
      body: JSON.stringify({ tasks, confirm_rights: true }),
    });
    const errors = payload.errors || [];
    const summary = `${payload.jobs.length} 个重新下载任务已加入队列`;
    toast(errors.length ? `${summary}；${errors.length} 个未加入：${errors[0].error}` : summary, errors.length > 0);
    for (const job of payload.jobs) state.selectedTasks.delete(job.target);
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    renderTasks(state.dashboard?.tasks || []);
  }
}

$("#deleteSelectedTasks").addEventListener("click", async () => {
  const selected = (state.dashboard?.tasks || [])
    .filter((task) => state.selectedTasks.has(task.task));
  const deletable = selected.filter((task) => !task.active_job);
  const activeCount = selected.length - deletable.length;
  if (!deletable.length) {
    return toast("选中的项目都在运行或排队，请先终止后再删除", true);
  }
  const phrase = `删除 ${deletable.length} 个项目`;
  const preview = deletable.slice(0, 5).map((task) => `• ${task.title}`).join("\n");
  const remainder = deletable.length > 5 ? `\n…以及另外 ${deletable.length - 5} 个项目` : "";
  const activeNote = activeCount ? `\n\n${activeCount} 个运行中或排队中的项目会保留。` : "";
  const confirmed = window.confirm(
    `将永久删除以下 ${deletable.length} 个视频项目、全部本地文件、作业记录和日志：\n\n${preview}${remainder}${activeNote}\n\n此操作无法撤销，确定继续吗？`,
  );
  if (!confirmed) return;
  const button = $("#deleteSelectedTasks");
  button.disabled = true;
  try {
    const payload = await api("/api/tasks/delete-batch", {
      method: "POST",
      body: JSON.stringify({
        tasks: deletable.map((task) => task.task),
        confirmation: phrase,
      }),
    });
    for (const task of payload.deleted_tasks || []) state.selectedTasks.delete(task);
    const summary = `已删除 ${payload.deleted} 个视频项目、${payload.files} 个文件，释放 ${formatBytes(payload.bytes)}`;
    toast(payload.failed ? `${summary}；${payload.failed} 个未删除` : summary, payload.failed > 0);
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});

$$("[data-workflow]").forEach((button) => {
  button.addEventListener("click", () => queueWorkflow(button.dataset.workflow));
});
$("#regenerateDubbing").addEventListener("click", () => queueWorkflow("dubbing", false, true));
$("#autoPublishSelected").addEventListener("click", () => queueWorkflow("complete", true, false, "publish"));

async function queueWorkflow(workflow, autoPublish = false, forceDubbing = false, automationTarget = "") {
  const selected = (state.dashboard?.tasks || [])
    .filter((task) => state.selectedTasks.has(task.task));
  const tasks = selected
    .filter((task) => !task.active_job)
    .map((task) => task.task);
  if (!tasks.length) {
    return toast(
      selected.length
        ? "选中的视频都已有运行中或排队中的作业"
        : "请先选择至少一个视频任务",
      true,
    );
  }
  const automation = automationSettingsSnapshot();
  if (automationTarget) automation.target = automationTarget;
  if (automation.target === "publish" && !["hardsub", "both"].includes(automation.renderMode)) {
    automation.renderMode = "hardsub";
  }
  const effectiveWorkflow = autoPublish && automation.target === "subtitles"
    ? "subtitles"
    : workflow;
  const chineseSource = autoPublish
    ? automation.chinesePolicy === "api_always" ? "deepseek" : "auto"
    : $("#chineseSubtitleSource").value;
  const dubbingEnabled = autoPublish
    ? automation.dubbingEnabled
    : (workflow === "dubbing" || $("#dubbingEnabled").checked);
  const dubbingReferenceMode = autoPublish
    ? automation.dubbingReferenceMode
    : $("#dubbingReferenceMode").value;
  const dubbingReferenceStart = autoPublish
    ? automation.dubbingReferenceStart
    : $("#dubbingReferenceStart").value;
  const dubbingReferenceEnd = autoPublish
    ? automation.dubbingReferenceEnd
    : $("#dubbingReferenceEnd").value;
  const dubbingSubtitleDisplay = autoPublish
    ? automation.dubbingSubtitleDisplay
    : $("#dubbingSubtitleDisplay").value;
  if (dubbingEnabled && chineseSource === "youtube_auto") {
    return toast("中文配音只使用现有 zh.reviewed.srt 或 AI 翻译生成的 zh.clean.srt", true);
  }
  if (dubbingEnabled && !state.dashboard?.health?.dubbing?.configured) {
    return toast("中文配音环境未就绪，请先配置独立运行时和本地 VoxCPM2 模型", true);
  }
  const needsApi = chineseSource === "deepseek"
    && (effectiveWorkflow === "subtitles" || effectiveWorkflow === "complete");
  if (!autoPublish && needsApi && !$("#paidApiConfirm").checked) {
    return toast("请先确认允许调用所选 AI API", true);
  }
  if (chineseSource === "youtube_auto") {
    const selected = (state.dashboard?.tasks || [])
      .filter((task) => state.selectedTasks.has(task.task));
    const missing = selected.filter((task) => !task.chinese_auto_available);
    if (missing.length) {
      const names = missing.slice(0, 3).map((task) => task.title).join("、");
      return toast(`${names} 没有自动生成的中文字幕，请改选 AI API 翻译`, true);
    }
  }
  try {
    const payload = await api("/api/pipeline", {
      method: "POST",
      body: JSON.stringify({
        tasks,
        workflow: effectiveWorkflow,
        render_mode: autoPublish
          ? automation.renderMode
          : $("#renderMode").value,
        chinese_subtitle_source: chineseSource,
        allow_paid_api: autoPublish
          ? automation.chinesePolicy !== "youtube_only"
          : $("#paidApiConfirm").checked,
        whisper_for_auto_subtitles: autoPublish
          ? automation.englishPolicy !== "youtube_first"
          : true,
        dubbing_enabled: dubbingEnabled,
        dubbing_reference_mode: dubbingReferenceMode,
        dubbing_reference_start: dubbingReferenceStart,
        dubbing_reference_end: dubbingReferenceEnd,
        dubbing_subtitle_display: dubbingSubtitleDisplay,
        force_dubbing: Boolean(forceDubbing),
        ...automationRequestValues(autoPublish, automation),
        ...coverRequestValues(autoPublish),
      }),
    });
    const targetLabels = { subtitles: "双语字幕", render: "双语成片", publish: "投稿" };
    toast(autoPublish
      ? `${payload.jobs.length} 个视频已进入无人值守${targetLabels[automation.target]}队列`
      : `${payload.jobs.length} 个处理任务已加入队列`);
    state.selectedTasks.clear();
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  }
}

function updateDubbingControls() {
  const enabled = $("#dubbingEnabled").checked;
  const manual = enabled && $("#dubbingReferenceMode").value === "manual";
  $("#dubbingReferenceMode").disabled = !enabled;
  $("#dubbingSubtitleDisplay").disabled = !enabled;
  $("#dubbingReferenceStartField").classList.toggle("hidden", !manual);
  $("#dubbingReferenceEndField").classList.toggle("hidden", !manual);
  $("#dubbingReferenceStart").disabled = !manual;
  $("#dubbingReferenceEnd").disabled = !manual;
  if (enabled && $("#chineseSubtitleSource").value === "youtube_auto") {
    $("#chineseSubtitleSource").value = "deepseek";
    updateChineseSourceControls();
  }
}

function updateChineseSourceControls() {
  const usesApi = $("#chineseSubtitleSource").value === "deepseek";
  const confirm = $("#paidApiConfirm");
  confirm.disabled = !usesApi;
  if (!usesApi) confirm.checked = false;
  confirm.closest(".paid-confirm").classList.toggle("disabled", !usesApi);
  const activeModel = state.dashboard?.health?.llm?.active?.model || "所选模型";
  $("#paidApiLabel").textContent = usesApi
    ? `允许调用 ${activeModel}`
    : "自动中文不调用 AI API";
}

$("#chineseSubtitleSource").addEventListener("change", updateChineseSourceControls);
$("#dubbingEnabled").addEventListener("change", updateDubbingControls);
$("#dubbingReferenceMode").addEventListener("change", updateDubbingControls);

const automationControls = [
  "#automationCoverChoice",
  "#automationCoverCloudAuthorized",
  "#autoPublishAfterDownload",
  "#automationTarget",
  "#automationEnglishPolicy",
  "#automationChinesePolicy",
  "#automationDubbingEnabled",
  "#automationDubbingReferenceMode",
  "#automationDubbingReferenceStart",
  "#automationDubbingReferenceEnd",
  "#automationDubbingSubtitleDisplay",
  "#automationDubbingReviewPolicy",
  "#automationRenderMode",
  "#automationFailurePolicy",
  "#automationSilentVideoPolicy",
  "#automationMetadataProvider",
  "#automationAccount",
  "#automationOnlySelf",
];
for (const selector of automationControls) {
  $(selector).addEventListener("change", () => {
    updateCoverRequestControls();
    updateAutomationFlow();
    saveAutomationSettings();
  });
}
updateChineseSourceControls();
updateDubbingControls();
try {
  const manualCover = localStorage.getItem("ytwf_manual_cover_choice");
  if (["off", "local", "cloud"].includes(manualCover)) $("#manualCoverChoice").value = manualCover;
} catch (_error) { /* Browser storage is optional. */ }
$("#manualCoverChoice").addEventListener("change", () => {
  updateCoverRequestControls();
  try { localStorage.setItem("ytwf_manual_cover_choice", $("#manualCoverChoice").value); } catch (_error) {}
});
updateCoverRequestControls();

async function prioritizePublishJob(button) {
  button.disabled = true;
  try {
    await api(`/api/jobs/${button.dataset.jobId}/prioritize`, {
      method: "POST",
      body: "{}",
    });
    toast("投稿已置顶；当前投稿完成后将自动接续");
    await refreshDashboard();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

async function movePublishJob(button) {
  button.disabled = true;
  try {
    await api(`/api/jobs/${button.dataset.jobId}/move`, {
      method: "POST",
      body: JSON.stringify({ direction: button.dataset.direction }),
    });
    toast("投稿顺序已调整");
    await refreshDashboard();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
}

$("#publishOrder").addEventListener("click", async (event) => {
  const moveButton = event.target.closest(".move-publish-job");
  if (moveButton) {
    await movePublishJob(moveButton);
    return;
  }
  const button = event.target.closest(".prioritize-job");
  if (button) await prioritizePublishJob(button);
});

$("#jobList").addEventListener("click", async (event) => {
  const resultButton = event.target.closest(".show-discovery-result");
  if (resultButton) {
    resultButton.disabled = true;
    try {
      await loadDiscoveryResult(resultButton.dataset.jobId);
      if ($("#logDialog").open) $("#logDialog").close();
    } catch (error) {
      resultButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
  const logButton = event.target.closest(".show-log");
  if (logButton) {
    state.activeLogJob = logButton.dataset.jobId;
    $("#logTitle").textContent = logButton.dataset.jobTitle;
    $("#logDialog").showModal();
    await refreshLog();
  }
  const retryButton = event.target.closest(".retry-job");
  if (retryButton) {
    retryButton.disabled = true;
    try {
      await api(`/api/jobs/${retryButton.dataset.jobId}/retry`, {
        method: "POST",
        body: "{}",
      });
      toast("任务已重新加入队列");
      await refreshDashboard();
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  const cancelButton = event.target.closest(".cancel-job");
  if (cancelButton) {
    if (!window.confirm("确定终止这个任务吗？\n已生成的文件会保留，终止后可重新加入队列。")) return;
    cancelButton.disabled = true;
    try {
      await api(`/api/jobs/${cancelButton.dataset.jobId}/cancel`, {
        method: "POST",
        body: "{}",
      });
      toast("已发送终止请求");
      await refreshDashboard();
    } catch (error) {
      cancelButton.disabled = false;
      toast(error.message, true);
    }
    return;
  }
  const deleteLogButton = event.target.closest(".delete-job-log");
  if (deleteLogButton) {
    if (!window.confirm("删除这条任务日志？作业记录会保留。")) return;
    deleteLogButton.disabled = true;
    try {
      const payload = await api(`/api/jobs/${deleteLogButton.dataset.jobId}/delete-log`, {
        method: "POST",
        body: "{}",
      });
      toast(payload.deleted ? `日志已删除，释放 ${formatBytes(payload.bytes)}` : "这条日志已经为空");
      await refreshDashboard();
    } catch (error) {
      deleteLogButton.disabled = false;
      toast(error.message, true);
    }
  }
});

async function refreshLog() {
  if (!state.activeLogJob || !$("#logDialog").open) return;
  try {
    const payload = await api(`/api/jobs/${state.activeLogJob}/log`);
    const pre = $("#logContent");
    const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 80;
    pre.textContent = payload.log || "任务尚未产生日志。";
    if (nearBottom) pre.scrollTop = pre.scrollHeight;
  } catch (error) {
    $("#logContent").textContent = error.message;
  }
}

function categoryOptions(categories) {
  const groups = new Map();
  for (const category of categories || []) {
    const parent = category.parent_name || "其他";
    if (!groups.has(parent)) groups.set(parent, []);
    groups.get(parent).push(category);
  }
  return [...groups.entries()].map(([parent, rows]) =>
    `<optgroup label="${escapeHtml(parent)}">${rows.map((row) =>
      `<option value="${Number(row.tid)}">${escapeHtml(row.name)} · TID ${Number(row.tid)}</option>`
    ).join("")}</optgroup>`
  ).join("");
}

async function openPublishDialog(task) {
  state.publishTask = task;
  const dialog = $("#publishDialog");
  $("#publishDialogTitle").textContent = "正在准备投稿信息…";
  $("#publishMediaState").textContent = "正在检查成片与账号…";
  $("#submitPublish").disabled = true;
  dialog.showModal();
  try {
    const defaults = await api(`/api/publish/defaults?task=${encodeURIComponent(task)}`);
    $("#publishDialogTitle").textContent = targetLabel(task);
    state.publishTitlePrefix = defaults.title_prefix || "";
    state.publishTitleBase = defaults.title.startsWith(state.publishTitlePrefix)
      ? defaults.title.slice(state.publishTitlePrefix.length)
      : defaults.title;
    state.publishAutoTitle = true;
    state.publishDynamicAuto = true;
    $("#publishTitlePrefix").value = state.publishTitlePrefix;
    $("#publishTitle").value = defaults.title;
    $("#publishTid").innerHTML = categoryOptions(defaults.categories);
    $("#publishTid").value = defaults.tid;
    $("#publishCopyright").value = String(defaults.copyright);
    $("#publishSubmit").value = defaults.submit;
    $("#publishLine").value = defaults.line;
    $("#publishLimit").value = defaults.limit;
    $("#publishSource").value = defaults.source;
    $("#publishTags").value = defaults.tags;
    $("#publishDescription").value = defaults.description;
    updatePublishDescriptionCount();
    $("#publishDynamic").value = defaults.dynamic;
    $("#publishOnlySelf").checked = defaults.is_only_self;
    $("#publishNoReprint").checked = defaults.no_reprint;
    $("#publishUseCover").checked = defaults.use_cover;
    state.publishPriority = state.publishPriorityDefault;
    state.publishPriorityDefault = false;
    $("#publishUseCover").disabled = !defaults.cover_available;
    $("#publishConfirm").checked = false;
    $("#publishAccount").innerHTML = defaults.accounts.length
      ? defaults.accounts.map((account) =>
          `<option value="${escapeHtml(account.id)}">${escapeHtml(account.label)} · ${escapeHtml(account.source)}</option>`
        ).join("")
      : '<option value="">未找到登录账号</option>';
    $("#publishAccount").value = defaults.account_id;
    const recommendation = $("#publishRecommendation");
    const recommended = defaults.metadata_status === "RECOMMENDED";
    recommendation.dataset.status = recommended ? "recommended" : "fallback";
    recommendation.querySelector(".recommendation-badge").textContent =
      recommended ? "DEEPSEEK 推荐" : "请人工核对";
    $("#publishRecommendedCategory").textContent =
      `${defaults.category_path} · TID ${defaults.tid}`;
    $("#publishRecommendationReason").textContent = [
      defaults.recommendation_reason,
      defaults.metadata_warning,
    ].filter(Boolean).join(" ");
    $("#publishMediaState").textContent = defaults.media_ready
      ? `投稿成片已就绪 · ${defaults.media_name}`
      : defaults.translation_ready
        ? `将先生成硬字幕 MP4 · ${defaults.media_name}`
        : "中文字幕尚未完成，暂时不能投稿";
    $("#submitPublish").disabled = !defaults.accounts.length || (!defaults.translation_ready && !defaults.media_ready);
    updateSourceRequirement();
  } catch (error) {
    $("#publishMediaState").textContent = error.message;
    $("#submitPublish").disabled = true;
    toast(error.message, true);
  }
}

function normalizePublishTitlePrefix(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function updatePublishTitlePrefix() {
  const prefixField = $("#publishTitlePrefix");
  const titleField = $("#publishTitle");
  if (!prefixField || !titleField) return;
  const oldPrefix = state.publishTitlePrefix || "";
  let title = state.publishAutoTitle ? state.publishTitleBase : titleField.value;
  if (!state.publishAutoTitle && oldPrefix && title.startsWith(oldPrefix)) {
    title = title.slice(oldPrefix.length);
  }
  const prefix = normalizePublishTitlePrefix(prefixField.value);
  titleField.value = `${prefix}${title}`;
  if (state.publishDynamicAuto) $("#publishDynamic").value = titleField.value;
  state.publishTitlePrefix = prefix;
}

function updateSourceRequirement() {
  const isReprint = $("#publishCopyright").value === "2";
  $("#publishSource").required = isReprint;
  $("#publishSource").closest(".field").querySelector("span").textContent =
    isReprint ? "转载来源（必填）" : "素材来源（可选）";
}

$("#publishCopyright").addEventListener("change", updateSourceRequirement);
$("#publishDescription").addEventListener("input", updatePublishDescriptionCount);
$("#publishTitlePrefix").addEventListener("input", updatePublishTitlePrefix);
$("#publishTitle").addEventListener("input", () => { state.publishAutoTitle = false; });
$("#publishDynamic").addEventListener("input", () => { state.publishDynamicAuto = false; });
$("#closePublish").addEventListener("click", () => $("#publishDialog").close());
$("#cancelPublish").addEventListener("click", () => $("#publishDialog").close());
$("#publishDialog").addEventListener("close", () => { state.publishTask = null; });

$("#publishForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.publishTask) return;
  const button = $("#submitPublish");
  button.disabled = true;
  try {
    const payload = await api("/api/publish", {
      method: "POST",
      body: JSON.stringify({
        task: state.publishTask,
        account_id: $("#publishAccount").value,
        title: $("#publishTitle").value,
        title_prefix: normalizePublishTitlePrefix($("#publishTitlePrefix").value),
        tid: Number($("#publishTid").value),
        copyright: Number($("#publishCopyright").value),
        submit: $("#publishSubmit").value,
        line: $("#publishLine").value,
        limit: Number($("#publishLimit").value),
        source: $("#publishSource").value,
        tags: $("#publishTags").value,
        description: $("#publishDescription").value,
        dynamic: $("#publishDynamic").value,
        is_only_self: $("#publishOnlySelf").checked,
        no_reprint: $("#publishNoReprint").checked,
        use_cover: $("#publishUseCover").checked,
        priority: state.publishPriority,
        confirm_publish: $("#publishConfirm").checked,
      }),
    });
    state.publishPriority = false;
    $("#publishDialog").close();
    const job = payload.job || {};
    toast(job.reused
      ? (job.queue_message || "这个视频已在投稿队列中，不会重复投稿")
      : job.priority
        ? "投稿任务已置顶；当前投稿完成后自动接续"
        : "投稿任务已加入队列；当前投稿完成后自动接续");
    await refreshDashboard();
  } catch (error) {
    button.disabled = false;
    toast(error.message, true);
  }
});

$("#closeLog").addEventListener("click", () => $("#logDialog").close());
$("#logDialog").addEventListener("close", () => { state.activeLogJob = null; });
$("#deleteCurrentLog").addEventListener("click", async () => {
  if (!state.activeLogJob) return;
  if (!window.confirm("删除当前显示的日志？作业记录会保留。")) return;
  const button = $("#deleteCurrentLog");
  button.disabled = true;
  try {
    const payload = await api(`/api/jobs/${state.activeLogJob}/delete-log`, {
      method: "POST",
      body: "{}",
    });
    $("#logDialog").close();
    toast(payload.deleted ? `日志已删除，释放 ${formatBytes(payload.bytes)}` : "这条日志已经为空");
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("#clearOldLogs").addEventListener("click", async () => {
  if (!window.confirm("清空所有已完成、失败和已取消任务的历史记录及日志？\n排队中和运行中的任务不会删除。")) return;
  const button = $("#clearOldLogs");
  button.disabled = true;
  try {
    const payload = await api("/api/logs/clear", {
      method: "POST",
      body: "{}",
    });
    toast(`已清理 ${payload.deleted_jobs} 条历史记录、${payload.deleted_logs} 个日志和 ${payload.deleted_results || 0} 个发现结果，释放 ${formatBytes(payload.bytes)}`);
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#openSetupButton").addEventListener("click", () => {
  state.setupDismissed = false;
  state.setupManuallyOpened = true;
  $("#setupGuide").classList.remove("hidden");
  $("#setupGuide").scrollIntoView({ behavior: "smooth", block: "start" });
});

$("#closeSetupButton").addEventListener("click", () => {
  state.setupDismissed = true;
  state.setupManuallyOpened = false;
  $("#setupGuide").classList.add("hidden");
});

$("#translationProviderSelect").addEventListener("change", (event) => {
  updateLlmProviderFields(event.target.value, "", "", true);
  $("#translationApiKeyInput").value = "";
});

function coverSettingsValues() {
  const values = {
    cover_enabled: $("#coverEnabled").checked,
    cover_allow_paid_copy: $("#coverAllowPaidCopy").checked,
  };
  if (state.dashboard?.health?.cover?.modes?.includes("cloud")) {
    Object.assign(values, {
      cover_mode: $("#coverMode").value,
      cover_allow_cloud_api: $("#coverAllowCloudApi").checked,
    });
  }
  return values;
}

$("#coverMode").addEventListener("change", () => {
  $("#coverCloudFields").classList.toggle("hidden", $("#coverMode").value !== "cloud");
});

$("#saveCoverSettings").addEventListener("click", async () => {
  if (!state.dashboard?.health?.cover) {
    toast("请在活动任务完成后重启控制面板后台", true);
    return;
  }
  const button = $("#saveCoverSettings");
  button.disabled = true;
  try {
    const result = await api("/api/settings", { method: "POST", body: JSON.stringify(coverSettingsValues()) });
    if (!(result.saved || []).includes("cover_enabled")) throw new Error("后台未保存封面开关，请重启面板后重试");
    state.coverInitialized = false;
    await refreshDashboard();
    toast("封面设置已保存，对新提交的任务生效");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = !state.dashboard?.health?.cover;
  }
});

$("#settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const youtube = $("#youtubeApiKeyInput").value.trim();
  const translationKey = $("#translationApiKeyInput").value.trim();
  const cookieFile = $("#youtubeCookiesInput").files[0];
  const provider = selectedLlmProvider();
  const body = {
    ...coverSettingsValues(),
    translation_provider: provider.id,
    translation_model: provider.custom_model
      ? $("#translationCustomModelInput").value.trim()
      : $("#translationModelSelect").value,
    translation_base_url: $("#translationBaseUrlInput").value.trim(),
    translation_thinking: $("#translationThinkingSelect").value,
    translation_batch_size: Number($("#translationBatchSize").value),
    translation_context_before: Number($("#translationContextBefore").value),
    translation_context_after: Number($("#translationContextAfter").value),
    translation_max_output_tokens: Number($("#translationMaxOutputTokens").value),
    discovery_llm_enabled: $("#discoveryLlmEnabled").checked,
    discovery_ollama_base_url: $("#discoveryOllamaBaseUrl").value.trim(),
    discovery_ollama_model: $("#discoveryOllamaModel").value.trim(),
    discovery_embedding_model: $("#discoveryEmbeddingModel").value.trim(),
    discovery_embedding_enabled: $("#discoveryEmbeddingEnabled").checked,
    discovery_query_planning_enabled: $("#discoveryQueryPlanningEnabled").checked,
    discovery_visual_enabled: $("#discoveryVisualEnabled").checked,
    discovery_metadata_batch_size: Number($("#discoveryMetadataBatchSize").value),
    discovery_visual_top_n: Number($("#discoveryVisualTopN").value),
    discovery_timeout_seconds: Number($("#discoveryTimeoutSeconds").value),
    discovery_thinking: $("#discoveryThinking").checked,
    discovery_recall_target: Number($("#discoveryRecallTarget").value),
    discovery_max_search_requests: Number($("#discoveryMaxSearchRequests").value),
    discovery_metadata_max_candidates: Number($("#discoveryMetadataMaxCandidates").value),
    publish_min_interval_minutes: Number($("#publishMinIntervalMinutes").value),
    publish_title_prefix: $("#defaultPublishTitlePrefix").value,
  };
  if (youtube) body.youtube_api_key = youtube;
  if (translationKey) body.translation_api_key = translationKey;
  const button = $("#saveSettingsButton");
  button.disabled = true;
  try {
    if (Object.keys(body).length) {
      await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    }
    if (cookieFile) {
      if (cookieFile.size > 4 * 1024 * 1024) throw new Error("Cookies 文件过大，最大允许 4 MB");
      const content = await cookieFile.text();
      await api("/api/youtube/cookies", {
        method: "POST",
        body: JSON.stringify({ action: "save", content }),
      });
    }
    $("#youtubeApiKeyInput").value = "";
    $("#translationApiKeyInput").value = "";
    $("#youtubeCookiesInput").value = "";
    toast("配置已安全保存到当前文件夹");
    state.llmInitialized = false;
    state.discoveryInitialized = false;
    state.publishingSettingsInitialized = false;
    state.coverInitialized = false;
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

async function clearSavedKey(field, label, extra = {}) {
  if (!window.confirm(`清除本机已保存的 ${label}？`)) return;
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ ...extra, [field]: "" }),
    });
    toast(`${label} 已清除`);
    state.llmInitialized = false;
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  }
}

$("#clearYoutubeKey").addEventListener("click", () => clearSavedKey("youtube_api_key", "YouTube API Key"));
$("#clearTranslationKey").addEventListener("click", () => {
  const provider = selectedLlmProvider();
  clearSavedKey(
    "translation_api_key",
    `${provider.label} API Key`,
    { translation_provider: provider.id },
  );
});

$("#clearYoutubeCookies").addEventListener("click", async () => {
  if (!window.confirm("清除本机已保存的 YouTube Cookie？需要登录验证的视频之后可能无法下载。")) return;
  try {
    await api("/api/youtube/cookies", {
      method: "POST",
      body: JSON.stringify({ action: "clear" }),
    });
    $("#youtubeCookiesInput").value = "";
    toast("YouTube Cookie 已清除");
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  }
});

$("#openBiliupLogin").addEventListener("click", async () => {
  const button = $("#openBiliupLogin");
  button.disabled = true;
  try {
    await api("/api/biliup/login", { method: "POST", body: "{}" });
    toast("登录工具已打开；登录完成后点击“重新检测”");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#recheckBiliup").addEventListener("click", async () => {
  await refreshDashboard(true);
  toast(state.dashboard?.health?.checks?.biliup_account ? "已检测到哔哩哔哩账号" : "暂未检测到账号，请先完成登录");
});

$("#refreshButton").addEventListener("click", () => refreshDashboard(true));

restoreAutomationSettings();
updateCoverRequestControls();
updateAutomationFlow();
loadDiscoveryCatalog();
refreshDashboard(true);
setInterval(() => refreshDashboard(false), 2500);
setInterval(refreshLog, 1500);
