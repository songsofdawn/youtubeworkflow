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
  renderReviewTask: null,
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

const AUTOMATION_SETTINGS_KEY = "youtube-workflow.automation.v1";

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
  renderTasks(dashboard.tasks);
  renderJobs(dashboard.jobs);
  updateChineseSourceControls();
}

function automationSettingsSnapshot() {
  return {
    enabled: $("#autoPublishAfterDownload").checked,
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
  try {
    settings = JSON.parse(localStorage.getItem(AUTOMATION_SETTINGS_KEY) || "null");
  } catch (_error) {
    settings = null;
  }
  if (!settings || typeof settings !== "object") return;
  $("#autoPublishAfterDownload").checked = settings.enabled === true;
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
  if (["block", "continue"].includes(settings.dubbingReviewPolicy)) {
    $("#automationDubbingReviewPolicy").value = settings.dubbingReviewPolicy;
  }
  if (["ass", "softsub", "hardsub", "both"].includes(settings.renderMode)) {
    $("#automationRenderMode").value = settings.renderMode;
  }
  if (["skip", "fail"].includes(settings.failurePolicy)) {
    $("#automationFailurePolicy").value = settings.failurePolicy;
  }
  if (["publish_original", "skip"].includes(settings.silentVideoPolicy)) {
    $("#automationSilentVideoPolicy").value = settings.silentVideoPolicy;
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
    : "当前关闭，只下载视频与原始字幕";
  const noSpeechOutcome = settings.target === "publish"
    && settings.silentVideoPolicy === "publish_original"
    ? "无可靠语音时保留原视频并生成中文投稿信息"
    : `无可靠语音时${settings.failurePolicy === "skip" ? "自动跳过" : "保留失败"}`;
  const englishDescriptions = {
    quality: `YouTube 字幕与 Whisper 自动比较，选择质量更高者；${noSpeechOutcome}`,
    youtube_first: `优先使用 YouTube 英文字幕；缺少时用 Whisper 兜底；${noSpeechOutcome}`,
    whisper: `忽略已有 YouTube 英文字幕，每个视频都强制使用本地 Whisper；${noSpeechOutcome}`,
  };
  const chineseDescriptions = {
    youtube_preferred: "优先可靠的 YouTube 中文；缺少或不可用时调用翻译 API",
    api_always: "忽略已有 YouTube 中文字幕；每个视频都调用所选 API 重新翻译",
    youtube_only: `只使用可靠的 YouTube 中文；缺少时${settings.failurePolicy === "skip" ? "自动跳过" : "保留失败"}`,
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
  const metadataLabel = $("#automationMetadataProvider").selectedOptions[0]?.textContent || "自动模型";
  const silentFlow = settings.silentVideoPolicy === "publish_original"
    ? "；无配音视频保留原画面与音轨，仅生成中文投稿信息"
    : "；无配音视频自动跳过";
  $("#automationMetadataFlow").textContent = `${metadataLabel}；自动填写标题、标签、简介和分区${silentFlow}`;
  const accountLabel = $("#automationAccount").selectedOptions[0]?.textContent || "自动选择账号";
  $("#automationPublishFlow").textContent = `${accountLabel} · ${settings.onlySelf ? "仅自己可见" : "公开投稿"}；瞬时网络错误自动重试并切换线路`;
  const omittedStages = settings.target === "subtitles"
    ? new Set(["dubbing", "render", "metadata", "publish"])
    : settings.target === "render" ? new Set(["metadata", "publish"]) : new Set();
  if (!settings.dubbingEnabled) omittedStages.add("dubbing");
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
  const automationButtonLabels = {
    subtitles: "按设置自动到字幕",
    render: "按设置自动到成片",
    publish: "按设置全自动投稿",
  };
  $("#autoPublishSelected").textContent = automationButtonLabels[settings.target];
  const reviewOutcome = !settings.dubbingEnabled
    ? ""
    : settings.dubbingReviewPolicy === "continue"
      ? " 中配时槽超限时仍会继续成片与投稿，请仅在接受重叠风险时使用。"
      : ` 中配时槽超限会在成片前阻止；随后${settings.failurePolicy === "skip" ? "自动跳过并继续队列" : "保留失败状态"}。`;
  $("#automationFailureFlow").textContent = (settings.failurePolicy === "skip"
    ? "异常策略：无法安全完成字幕或成片时，记录原因并自动跳过该视频；其他视频继续执行。"
    : "异常策略：无法安全完成字幕或成片时，将该视频保留为失败状态；不会上传不合格成片。")
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

function automationRequestValues(autoPublish) {
  const settings = automationSettingsSnapshot();
  return {
    auto_publish: Boolean(autoPublish),
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
    ...(autoPublish ? {
      dubbing_enabled: settings.dubbingEnabled,
      dubbing_reference_mode: settings.dubbingReferenceMode,
      dubbing_reference_start: settings.dubbingReferenceStart,
      dubbing_reference_end: settings.dubbingReferenceEnd,
      dubbing_subtitle_display: settings.dubbingSubtitleDisplay,
      force_dubbing: false,
    } : {}),
  };
}

function updateSetupStatus(selector, configured, readyText, missingText) {
  const element = $(selector);
  element.textContent = configured ? readyText : missingText;
  element.classList.toggle("configured", configured);
}

function renderSetupGuide(health) {
  const checks = health.checks || {};
  updateSetupStatus("#youtubeSetupStatus", checks.youtube_api, "已配置", "未配置");
  updateSetupStatus("#youtubeCookiesSetupStatus", checks.youtube_cookies, "已导入", "未导入");
  updateSetupStatus("#translationSetupStatus", checks.translation_api, "已配置", "未配置");
  updateSetupStatus("#discoveryLlmSetupStatus", checks.discovery_llm, "已连接", health.discovery?.enabled ? "未连接" : "未启用");
  updateSetupStatus("#biliupSetupStatus", checks.biliup_account, "已登录", "未登录");
  renderLlmSettings(health.llm);
  renderDiscoverySettings(health.discovery);
  renderPublishingSettings(health.publishing);
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

const stageNames = { download: "下载", english: "英文", translation: "AI 翻译", dubbing: "配音", render: "成片", publish: "投稿" };

function renderTasks(tasks) {
  const taskKeys = new Set(tasks.map((task) => task.task));
  for (const selected of [...state.selectedTasks]) {
    if (!taskKeys.has(selected)) state.selectedTasks.delete(selected);
  }
  $("#selectedTaskCount").textContent = state.selectedTasks.size;
  const selectedRows = tasks.filter((task) => state.selectedTasks.has(task.task));
  const deletableCount = selectedRows.filter((task) => !task.active_job).length;
  const activeCount = selectedRows.length - deletableCount;
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
    const subtitle = active
      ? active.status === "queued" ? "队列中" : "执行中"
      : reviewSummary || `${task.progress}%`;
    const stages = Object.entries(task.stages).map(([key, value]) =>
      `<span class="stage ${escapeHtml(value.state)}" title="${escapeHtml(value.detail)}">${stageNames[key]}</span>`
    ).join("");
    const image = task.thumbnail_url
      ? `<img class="task-thumb" src="${escapeHtml(task.thumbnail_url)}" alt="" loading="lazy">`
      : `<div class="task-thumb"></div>`;
    const publishAction = active ? "" : task.stages.publish.state === "complete"
      ? task.bilibili_url
        ? `<button class="icon-button open-bilibili" type="button" title="打开B站稿件" aria-label="打开B站稿件">B</button>`
        : ""
      : task.stages.publish.state === "active"
        ? ""
      : task.stages.render.state === "complete"
          ? `<button class="icon-button publish-task" type="button" title="投稿到哔哩哔哩" aria-label="投稿到哔哩哔哩">↑</button>`
          : "";
    const layoutReview = !active
      && task.stages.publish.state !== "complete"
      && task.stages.render.state === "review"
      && task.review?.code === "SUBTITLE_LAYOUT_REVIEW_REQUIRED";
    const renderAction = layoutReview
      ? `<button class="icon-button review-task" type="button" title="审核过长字幕并继续成片" aria-label="审核过长字幕并继续成片">审</button>`
      : !active
        && task.stages.publish.state !== "complete"
        && task.stages.translation.state === "complete"
        && task.stage4_status !== "STAGE4_COMPLETED"
        ? `<button class="icon-button render-task" type="button" title="仅重新成片" aria-label="仅重新成片">▶</button>`
        : "";
    return `
      <article class="task-row ${selected ? "selected" : ""}" data-task="${escapeHtml(task.task)}" data-video-id="${escapeHtml(task.video_id)}" data-title="${escapeHtml(task.title)}" data-bilibili-url="${escapeHtml(task.bilibili_url || "")}">
        <input class="task-check" type="checkbox" aria-label="选择 ${escapeHtml(task.title)}" ${selected ? "checked" : ""}>
        ${image}
        <div class="task-title">
          <strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong>
          <span>${escapeHtml(task.channel || task.video_id || task.task)}</span>
          <small class="${task.chinese_auto_available ? "available" : "missing"}">
            ${task.chinese_auto_available
              ? `YouTube 自动中文可用 · ${escapeHtml(task.chinese_auto_name)}`
              : "没有 YouTube 自动中文字幕"}
          </small>
        </div>
        <div class="stage-track">${stages}</div>
        <div class="status-cell">
          <strong>${escapeHtml(task.overall)}</strong>
          <small class="${summaryClass}" title="${escapeHtml(reviewSummary)}">${escapeHtml(subtitle)}</small>
          <progress class="progress-mini" max="100" value="${Math.max(0, Math.min(100, progress))}" aria-label="进度 ${progress}%"></progress>
        </div>
        <div class="task-actions">
          ${active
            ? `<button class="icon-button danger cancel-task-job" type="button" data-job-id="${escapeHtml(active.id)}" title="终止当前进程" aria-label="终止当前进程">■</button>`
            : ""}
          ${publishAction}
          ${renderAction}
          ${task.dubbing_available
            ? '<button class="icon-button open-dubbing-folder" type="button" title="打开中文配音目录" aria-label="打开中文配音目录">音</button>'
            : ""}
          <button class="icon-button open-folder" type="button" title="打开任务目录" aria-label="打开任务目录">↗</button>
          <button class="icon-button danger delete-task" type="button" title="${active ? "请先终止运行中的任务" : "删除视频任务及全部文件"}" aria-label="删除视频任务及全部文件" ${active ? "disabled" : ""}>×</button>
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
      : job.kind === "publish" ? "PUBLISH"
        : job.kind === "discovery" ? "DISCOVERY"
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
  const discoveryMeta = state.discoveryPayload ? `
    <div class="discovery-score-row">
      <strong>机会分 ${Number(item.opportunity_score || 0).toFixed(1)}</strong>
      <span>${item.llm_status === "scored" ? "Qwen 已评审" : "规则评分"}${qualityTier} · 发布 ${discoveryAge(item.age_hours)}</span>
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
  const limitPerPack = Number(summary.result_limit_per_pack || summary.result_target_per_pack || state.discoveryPayload.per_pack || 0);
  const recalledByPack = summary.recalled_counts_by_pack || {};
  const recalledAssignmentCount = Object.values(recalledByPack)
    .reduce((total, value) => total + Number(value || 0), 0);
  const uniqueResultCount = Number(summary.unique_result_count ?? summary.result_count ?? 0);
  const assignmentCount = Number(summary.result_count ?? state.searchResults.length);
  const qualityEligibleCount = Number(summary.preferred_eligible_count ?? summary.selection_eligible_count ?? visible.length);
  const expandedResultCount = Number(summary.expanded_result_count || 0);
  const reserveResultCount = Number(summary.reserve_result_count || 0);
  const uniqueResultNote = uniqueResultCount < assignmentCount
    ? `（${uniqueResultCount} 个不重复视频）`
    : "";
  $("#discoveryResultSummary").textContent =
    `${summary.selected_pack_count || 0} 个领域 · 每领域目标 ${limitPerPack} 条 · `
    + `时长 ${Math.round(Number(summary.minimum_duration_seconds || 0) / 60)}–${Math.round(Number(summary.maximum_duration_seconds || 0) / 60)} 分钟 · `
    + `各领域召回 ${recalledAssignmentCount}/${summary.recall_target || 0} 条、去重 ${summary.raw_candidate_count || 0} 条（搜索 ${summary.search_request_count || 0}/${summary.search_request_limit || 0} 次） → `
    + `规则保留 ${summary.eligible_count || 0} 条 → AI ${summary.llm_scored_count || 0}/${summary.llm_candidate_count || 0} 条 → 优选 ${qualityEligibleCount} 条 → `
    + `最终 ${assignmentCount} 个领域候选位${uniqueResultNote}（扩展优选 ${expandedResultCount} 条、补量备选 ${reserveResultCount} 条）、当前显示 ${visible.length} 条 · 视觉复评 ${summary.visual_scored_count || 0} 条`;

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
    warningItems.push(`以下领域在 YouTube 搜索阶段没有召回结果：${zeroRecallLabels.join("、")}。程序已记录每领域搜索产出，便于继续扩充关键词。`);
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
        ? "本领域的搜索词没有召回视频；不是 AI 筛选后变成 0。"
        : `本领域没有通过硬性安全条件的候选；优选 ${eligibleCount} 条。`;
    return `
      <section class="result-group" data-pack-id="${escapeHtml(group.id)}">
        <div class="result-group-heading">
          <div><h3>${escapeHtml(group.label)}</h3><p>${escapeHtml(group.description)} · 候选 ${actualCount}/目标 ${limitPerPack}（补量备选 ${reserveCount}） · 当前显示 ${rows.length} 条</p></div>
          ${rows.length ? '<button class="button button-ghost button-small select-discovery-group" type="button">选择本区</button>' : ""}
        </div>
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
  if (!packs.length) return toast("请至少选择一个发现领域", true);
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
        ...automationRequestValues($("#autoPublishAfterDownload").checked),
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
        ...automationRequestValues($("#autoPublishAfterDownload").checked),
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

$("#taskList").addEventListener("click", async (event) => {
  const row = event.target.closest(".task-row");
  if (!row) return;
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
  }
  if (event.target.closest(".open-bilibili") && row.dataset.bilibiliUrl) {
    window.open(row.dataset.bilibiliUrl, "_blank", "noopener,noreferrer");
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
$("#autoPublishSelected").addEventListener("click", () => queueWorkflow("complete", true));

async function queueWorkflow(workflow, autoPublish = false, forceDubbing = false) {
  const tasks = [...state.selectedTasks];
  if (!tasks.length) return toast("请先选择至少一个视频任务", true);
  const automation = automationSettingsSnapshot();
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
        ...automationRequestValues(autoPublish),
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
    updateAutomationFlow();
    saveAutomationSettings();
  });
}
updateChineseSourceControls();
updateDubbingControls();

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

function updateSourceRequirement() {
  const isReprint = $("#publishCopyright").value === "2";
  $("#publishSource").required = isReprint;
  $("#publishSource").closest(".field").querySelector("span").textContent =
    isReprint ? "转载来源（必填）" : "素材来源（可选）";
}

$("#publishCopyright").addEventListener("change", updateSourceRequirement);
$("#publishDescription").addEventListener("input", updatePublishDescriptionCount);
$("#closePublish").addEventListener("click", () => $("#publishDialog").close());
$("#cancelPublish").addEventListener("click", () => $("#publishDialog").close());
$("#publishDialog").addEventListener("close", () => { state.publishTask = null; });

$("#publishForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.publishTask) return;
  const button = $("#submitPublish");
  button.disabled = true;
  try {
    await api("/api/publish", {
      method: "POST",
      body: JSON.stringify({
        task: state.publishTask,
        account_id: $("#publishAccount").value,
        title: $("#publishTitle").value,
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
        confirm_publish: $("#publishConfirm").checked,
      }),
    });
    $("#publishDialog").close();
    toast("投稿任务已加入队列");
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

$("#settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const youtube = $("#youtubeApiKeyInput").value.trim();
  const translationKey = $("#translationApiKeyInput").value.trim();
  const cookieFile = $("#youtubeCookiesInput").files[0];
  const provider = selectedLlmProvider();
  const body = {
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
updateAutomationFlow();
loadDiscoveryCatalog();
refreshDashboard(true);
setInterval(() => refreshDashboard(false), 2500);
setInterval(refreshLog, 1500);
