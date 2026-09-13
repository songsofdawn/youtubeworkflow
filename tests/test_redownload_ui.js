const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../src/control_panel/static/app.js"), "utf8");
function between(start, end) {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first);
  return source.slice(first, last);
}

function fixture() {
  const elements = {};
  const calls = [];
  const rows = [
    { task: "manual/old/failed", title: "Failed", video_id: "abcdefghijk", progress: 0,
      stages: { download: { state: "failed" }, publish: { state: "pending" },
        render: { state: "pending" }, translation: { state: "pending" } } },
    { task: "manual/old/running", title: "Running", video_id: "12345678901", progress: 0,
      active_job: { id: "active", status: "queued", progress: 0 },
      stages: { download: { state: "failed" }, publish: { state: "pending" },
        render: { state: "pending" }, translation: { state: "pending" } } },
  ];
  const context = vm.createContext({
    state: { dashboard: { tasks: rows }, selectedTasks: new Set(rows.map((row) => row.task)) },
    $: (selector) => elements[selector] ||= {},
    escapeHtml: (value) => String(value || ""),
    stageNames: { download: "下载", publish: "投稿", render: "成片", translation: "翻译" },
    window: { confirm: () => true },
    toast: (...args) => calls.push(["toast", ...args]),
    api: async (url, options) => {
      calls.push(["api", url, JSON.parse(options.body)]);
      return { jobs: [{ target: rows[0].task }], errors: [] };
    },
    refreshDashboard: async () => { calls.push(["refresh"]); },
  });
  vm.runInContext(
    between("function renderTasks(tasks)", "function renderJobs(jobs)") +
    between("async function queueRedownloads(tasks, button)", '$("#deleteSelectedTasks").addEventListener'),
    context,
  );
  return { context, elements, calls, rows };
}

test("task cards and batch controls only enable idle tasks", () => {
  const { context, elements, rows } = fixture();
  context.renderTasks(rows);
  const buttons = elements["#taskList"].innerHTML.match(/<button[^>]+redownload-task[^>]*>/g);
  assert.equal(buttons.length, 2);
  assert.ok(!buttons[0].includes("disabled"));
  assert.ok(buttons[1].includes("disabled"));
  assert.equal(elements["#redownloadSelectedTasks"].textContent, "重新下载 (1)");
  context.state.selectedTasks = new Set([rows[1].task]);
  context.renderTasks(rows);
  assert.equal(elements["#redownloadSelectedTasks"].disabled, true);
});

test("confirmation queues the exact task and keeps rejected selections", async () => {
  const { context, calls, rows } = fixture();
  await context.queueRedownloads(rows.map((row) => row.task), {});
  assert.deepEqual(calls[0], ["api", "/api/tasks/redownload", {
    tasks: rows.map((row) => row.task), confirm_rights: true,
  }]);
  assert.equal(context.state.selectedTasks.has(rows[0].task), false);
  assert.equal(context.state.selectedTasks.has(rows[1].task), true);
  assert.ok(calls.some((call) => call[0] === "refresh"));
});

test("canceling the rights confirmation performs no request", async () => {
  const { context, calls, rows } = fixture();
  context.window.confirm = () => false;
  await context.queueRedownloads([rows[0].task], {});
  assert.equal(calls.length, 0);
  assert.equal(context.state.selectedTasks.size, 2);
});

test("batch action skips selected active jobs", async () => {
  const { context, calls, rows, elements } = fixture();
  let callback;
  elements["#redownloadSelectedTasks"] = { addEventListener: (_event, listener) => { callback = listener; } };
  vm.runInContext(between('$("#redownloadSelectedTasks").addEventListener', "async function queueRedownloads"), context);
  await callback();
  const request = calls.find((call) => call[0] === "api");
  assert.deepEqual(request[2].tasks, [rows[0].task]);
});

test("request failures keep selections and restore controls", async () => {
  const { context, calls, rows } = fixture();
  context.api = async () => { throw new Error("server unavailable"); };
  const button = {};
  await context.queueRedownloads([rows[0].task], button);
  assert.equal(button.disabled, false);
  assert.equal(context.state.selectedTasks.size, 2);
  assert.ok(calls.some((call) => call[0] === "toast" && call[1] === "server unavailable" && call[2]));
});
