"use strict";

const state = {
  dashboard: null,
  searchResults: [],
  selectedResults: new Set(),
  selectedTasks: new Set(),
  activeLogJob: null,
  refreshBusy: false,
};

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

function targetLabel(target) {
  const parts = String(target || "").split(/[\\/]/);
  return parts.at(-1) || target || "未命名任务";
}

async function refreshDashboard(showError = false) {
  if (state.refreshBusy) return;
  state.refreshBusy = true;
  try {
    const dashboard = await api("/api/dashboard");
    state.dashboard = dashboard;
    renderDashboard(dashboard);
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
  $("#metricCompleted").textContent = summary.completed;
  renderHealth(dashboard.health);
  renderTasks(dashboard.tasks);
  renderJobs(dashboard.jobs);
}

function renderHealth(health) {
  const labels = {
    download_environment: "下载运行环境",
    stage3_environment: "字幕与成片环境",
    tools: "yt-dlp / FFmpeg 工具",
    whisper_model: "本地 Whisper 模型",
    youtube_api: "YouTube API 密钥",
    deepseek_api: "DeepSeek API 密钥",
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
}

const stageNames = { download: "下载", english: "英文", translation: "翻译", render: "成片" };

function renderTasks(tasks) {
  const taskKeys = new Set(tasks.map((task) => task.task));
  for (const selected of [...state.selectedTasks]) {
    if (!taskKeys.has(selected)) state.selectedTasks.delete(selected);
  }
  $("#selectedTaskCount").textContent = state.selectedTasks.size;
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
    const subtitle = active ? active.status === "queued" ? "队列中" : "执行中" : `${task.progress}%`;
    const stages = Object.entries(task.stages).map(([key, value]) =>
      `<span class="stage ${escapeHtml(value.state)}" title="${escapeHtml(value.detail)}">${stageNames[key]}</span>`
    ).join("");
    const image = task.thumbnail_url
      ? `<img class="task-thumb" src="${escapeHtml(task.thumbnail_url)}" alt="" loading="lazy">`
      : `<div class="task-thumb"></div>`;
    return `
      <article class="task-row ${selected ? "selected" : ""}" data-task="${escapeHtml(task.task)}">
        <input class="task-check" type="checkbox" aria-label="选择 ${escapeHtml(task.title)}" ${selected ? "checked" : ""}>
        ${image}
        <div class="task-title">
          <strong title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</strong>
          <span>${escapeHtml(task.channel || task.video_id || task.task)}</span>
        </div>
        <div class="stage-track">${stages}</div>
        <div class="status-cell">
          <strong>${escapeHtml(task.overall)}</strong>
          <small>${subtitle}</small>
          <progress class="progress-mini" max="100" value="${Math.max(0, Math.min(100, progress))}" aria-label="进度 ${progress}%"></progress>
        </div>
        <div class="task-actions">
          ${task.stages.translation.state === "complete" && task.stage4_status !== "STAGE4_COMPLETED"
            ? `<button class="icon-button render-task" type="button" title="仅重新成片" aria-label="仅重新成片">▶</button>`
            : ""}
          <button class="icon-button open-folder" type="button" title="打开任务目录" aria-label="打开任务目录">↗</button>
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
    const retry = job.status === "failed"
      ? `<button class="button button-ghost retry-job" type="button" data-job-id="${job.id}">重试</button>`
      : "";
    const kind = job.kind === "download" ? "DOWNLOAD" : "PIPELINE";
    const status = {
      queued: "等待中",
      running: "运行中",
      completed: "已完成",
      failed: "失败",
      cancelled: "已取消",
    }[job.status] || job.status;
    return `
      <article class="job-item">
        <div class="job-top"><span class="job-kind">${kind}</span><span class="job-status">${status} · ${job.progress}%</span></div>
        <strong title="${escapeHtml(job.target)}">${escapeHtml(targetLabel(job.target))}</strong>
        <p>${escapeHtml(job.step)}${job.error ? ` · ${escapeHtml(job.error)}` : ""}</p>
        <progress class="progress-mini" max="100" value="${job.progress}" aria-label="进度 ${job.progress}%"></progress>
        <div class="job-actions">
          <button class="button button-ghost show-log" type="button" data-job-id="${job.id}" data-job-title="${escapeHtml(targetLabel(job.target))}">查看日志</button>
          ${retry}
        </div>
      </article>`;
  }).join("");
}

function renderSearchResults() {
  const section = $("#searchResultsSection");
  section.classList.toggle("hidden", !state.searchResults.length);
  $("#searchResultsTitle").textContent = `搜索结果 · ${state.searchResults.length}`;
  $("#searchResults").innerHTML = state.searchResults.map((item) => {
    const selected = state.selectedResults.has(item.video_id);
    return `
      <article class="result-card ${selected ? "selected" : ""}" data-video-id="${item.video_id}">
        <label class="result-select">
          <input class="result-check" type="checkbox" aria-label="选择 ${escapeHtml(item.title)}" ${selected ? "checked" : ""}>
        </label>
        <img src="${escapeHtml(item.thumbnail_url)}" alt="" loading="lazy">
        <div class="result-body">
          <h3 title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</h3>
          <p>${escapeHtml(item.channel_title)}</p>
          <div class="result-meta">
            <span>${escapeHtml(item.duration)}</span>
            <span>${number(item.view_count)} 次观看</span>
            <span>${item.has_caption ? "有字幕" : "无字幕"}</span>
          </div>
        </div>
      </article>`;
  }).join("");
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

$("#selectAllResults").addEventListener("click", () => {
  const allSelected = state.selectedResults.size === state.searchResults.length;
  state.selectedResults = allSelected
    ? new Set()
    : new Set(state.searchResults.map((item) => item.video_id));
  renderSearchResults();
});

$("#downloadResults").addEventListener("click", async () => {
  const items = state.searchResults.filter((item) => state.selectedResults.has(item.video_id));
  if (!items.length) return toast("请先选择要下载的视频", true);
  const button = $("#downloadResults");
  button.disabled = true;
  try {
    const payload = await api("/api/downloads", {
      method: "POST",
      body: JSON.stringify({
        items,
        confirm_rights: $("#searchRights").checked,
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
  if (event.target.closest(".render-task")) {
    state.selectedTasks = new Set([row.dataset.task]);
    await queueWorkflow("render");
  }
});

$("#selectAllTasks").addEventListener("click", () => {
  const tasks = state.dashboard?.tasks || [];
  const allSelected = tasks.length && state.selectedTasks.size === tasks.length;
  state.selectedTasks = allSelected ? new Set() : new Set(tasks.map((task) => task.task));
  renderTasks(tasks);
});

$$("[data-workflow]").forEach((button) => {
  button.addEventListener("click", () => queueWorkflow(button.dataset.workflow));
});

async function queueWorkflow(workflow) {
  const tasks = [...state.selectedTasks];
  if (!tasks.length) return toast("请先选择至少一个视频任务", true);
  const needsPaid = workflow === "subtitles" || workflow === "complete";
  if (needsPaid && !$("#paidApiConfirm").checked) {
    return toast("请先确认允许 DeepSeek 付费翻译", true);
  }
  try {
    const payload = await api("/api/pipeline", {
      method: "POST",
      body: JSON.stringify({
        tasks,
        workflow,
        render_mode: $("#renderMode").value,
        allow_paid_api: $("#paidApiConfirm").checked,
      }),
    });
    toast(`${payload.jobs.length} 个处理任务已加入队列`);
    state.selectedTasks.clear();
    await refreshDashboard();
  } catch (error) {
    toast(error.message, true);
  }
}

$("#jobList").addEventListener("click", async (event) => {
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

$("#closeLog").addEventListener("click", () => $("#logDialog").close());
$("#logDialog").addEventListener("close", () => { state.activeLogJob = null; });
$("#refreshButton").addEventListener("click", () => refreshDashboard(true));

refreshDashboard(true);
setInterval(() => refreshDashboard(false), 2500);
setInterval(refreshLog, 1500);
