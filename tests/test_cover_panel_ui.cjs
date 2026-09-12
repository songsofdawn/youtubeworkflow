// Offline browser-state regression: run with node tests/test_cover_panel_ui.cjs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/control_panel/static/app.js'), 'utf8');
const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, {
    textContent: '', value: '', disabled: false, checked: false,
    classList: { add() {}, remove() {}, toggle() {} },
    removeAttribute() {}, getAttribute() { return null; },
    listeners: {},
    addEventListener(event, handler) { this.listeners[event] = handler; },
    scrollIntoView() {}, focus() {},
  });
  return elements.get(id);
};
let requests = 0;
const submitted = [];
const messages = [];
const state = { dashboard: { health: {}, tasks: [{ task: 'fixture', title: 'Test', stages: {} }] },
  coverPreviewTask: 'fixture', coverInitialized: true };
const context = vm.createContext({ $, state,
  api: async (url, options) => {
    requests++;
    if (options?.body) submitted.push({ url, body: JSON.parse(options.body) });
    return { log: 'fixture log', jobs: [] };
  },
  toast: (message) => messages.push(message),
  refreshDashboard: async () => {},
  visibleSearchResults: () => state.searchResults,
  updateSetupStatus: (id, ready, yes, no) => { $(id).textContent = ready ? yes : no; },
  renderLlmSettings() {}, renderDiscoverySettings() {}, renderPublishingSettings() {},
});
vm.runInContext(source.slice(source.indexOf('function renderSetupGuide('),
  source.indexOf('function renderPublishingSettings(')), context);
vm.runInContext(source.slice(source.indexOf('async function updateCoverPreview('),
  source.indexOf('$("#closeCoverPreview").addEventListener')), context);
vm.runInContext(source.slice(source.indexOf('function coverSettingsValues('),
  source.indexOf('$("#coverMode").addEventListener')), context);
vm.runInContext(source.slice(source.indexOf('function coverRequestValues('),
  source.indexOf('function automationRequestValues(')), context);
context.automationSettingsSnapshot = () => ({
  target: 'render', renderMode: 'hardsub', chinesePolicy: 'youtube_only',
  englishPolicy: 'youtube_first', dubbingEnabled: false,
});
vm.runInContext(source.slice(source.indexOf('function automationRequestValues('),
  source.indexOf('function updateSetupStatus(')), context);
vm.runInContext(source.slice(source.indexOf('$("#directForm").addEventListener'),
  source.indexOf('$("#searchResults").addEventListener')), context);
vm.runInContext(source.slice(source.indexOf('$("#downloadResults").addEventListener'),
  source.indexOf('$("#taskList").addEventListener')), context);
vm.runInContext(source.slice(source.indexOf('$("#autoPublishSelected").addEventListener'),
  source.indexOf('function updateDubbingControls(')), context);
(async () => {
  // Restored consent must survive initial loading and a temporarily missing Key.
  $('#automationCoverChoice').value = 'cloud';
  $('#automationCoverCloudAuthorized').checked = true;
  context.updateCoverRequestControls(null);
  assert.equal($('#automationCoverCloudAuthorized').disabled, false);
  assert.equal($('#automationCoverCloudAuthorized').checked, true);
  context.renderSetupGuide({});
  assert.equal($('#automationCoverCloudAuthorized').disabled, false);
  assert.equal($('#automationCoverCloudAuthorized').checked, true);
  assert.match($('#automationCoverAuthorizationHint').textContent, /Key/);
  assert.equal($('#coverSetupStatus').textContent, '需重启后台');
  assert.equal($('#coverEnabled').disabled, true);
  assert.equal(state.coverInitialized, false);
  await context.updateCoverPreview();
  assert.equal(requests, 0, 'old backend must not receive unsupported cover requests');
  assert.match($('#coverOriginalHint').textContent, /不能据此判断原图缺失/);
  assert.equal($('#regenerateCover').disabled, true);
  state.dashboard.health.cover = { enabled: true, pillow_ready: true };
  context.renderSetupGuide(state.dashboard.health);
  assert.equal($('#automationCoverCloudAuthorized').disabled, false);
  assert.equal($('#coverEnabled').checked, true);
  assert.equal($('#coverEnabled').disabled, false);
  assert.equal($('#coverSetupStatus').textContent, '已启用');
  state.dashboard.tasks[0].cover_original_available = true;
  await context.updateCoverPreview();
  assert.match($('#coverOriginalPreview').src, /variant=original/);
  assert.equal($('#regenerateCover').disabled, false);
  $('#coverOriginalPreview').onerror();
  assert.match($('#coverOriginalHint').textContent, /加载失败/);
  state.coverInitialized = false;
  Object.assign(state.dashboard.health.cover, { mode: 'cloud', modes: ['local', 'cloud'],
    allow_cloud_api: false, api_key_configured: true,
    api_provider_label: '智谱 GLM', api_model: 'glm-4.7-flash' });
  context.renderSetupGuide(state.dashboard.health);
  assert.doesNotMatch($('#automationCoverAuthorizationHint').textContent, /需.*Key/);
  assert.equal($('#coverMode').value, 'cloud');
  assert.equal($('#coverMode').disabled, false);
  assert.equal($('#coverSetupStatus').textContent, '等待 API 授权');
  $('#coverAllowCloudApi').checked = true;
  const values = context.coverSettingsValues();
  assert.equal(values.cover_mode, 'cloud');
  assert.equal(values.cover_allow_cloud_api, true);
  assert.equal(values.cover_cloud_api_key, undefined);
  assert.match($('#coverCloudKeyStatus').textContent, /智谱 GLM/);
  await context.updateCoverPreview();
  assert.equal($('#coverRegeneratePaid').disabled, true);
  assert.equal($('#coverRegenerateCloud').disabled, false);
  assert.match($('#coverPreviewMode').textContent, /Pillow/);
  state.dashboard.health.cover.per_job_options = true;
  $('#automationCoverChoice').value = 'cloud';
  $('#automationCoverCloudAuthorized').checked = true;
  $('#manualCoverChoice').value = 'off';
  $('#manualCoverCloudAuthorized').checked = false;
  assert.equal(context.coverRequestValues(true).cover_choice, 'cloud');
  assert.equal(context.coverRequestValues(true).cover_cloud_authorized, true);
  assert.equal(context.coverRequestValues(false).cover_choice, 'off');
  $('#manualCoverChoice').value = 'cloud';
  assert.throws(() => context.coverRequestValues(false), /手动/);
  // Direct and selected-result downloads must ignore both cloud selections.
  $('#automationCoverCloudAuthorized').checked = false;
  $('#autoPublishAfterDownload').checked = false;
  $('#directInput').value = 'abcdefghijk';
  $('#directRights').checked = true;
  await $('#directForm').listeners.submit({ preventDefault() {}, currentTarget: {} });
  state.searchResults = [{ video_id: 'abcdefghijk', youtube_url: 'https://www.youtube.com/watch?v=abcdefghijk' }];
  state.selectedResults = new Set(['abcdefghijk']);
  $('#searchRights').checked = true;
  await $('#downloadResults').listeners.click();
  assert.equal(submitted.length, 2);
  for (const request of submitted.slice(-2)) {
    assert.equal(request.url, '/api/downloads');
    assert.equal(request.body.auto_publish, false);
    assert.equal(request.body.cover_choice, 'off');
    assert.equal(request.body.cover_enabled, false);
    assert.equal(request.body.cover_cloud_authorized, false);
    assert.equal(request.body.confirm_rights, true);
  }
  // Enabling automation still enforces consent for an explicitly chosen API cover.
  $('#autoPublishAfterDownload').checked = true;
  assert.throws(() => context.downloadRequestValues(), /授权.*关闭封面/);
  $('#automationCoverCloudAuthorized').checked = true;
  assert.equal(context.downloadRequestValues().cover_choice, 'cloud');
  assert.equal(context.downloadRequestValues().cover_cloud_authorized, true);
  // One-click publishing uses the automation cover, even with download automation off.
  $('#autoPublishAfterDownload').checked = false;
  state.selectedTasks = new Set(['fixture']);
  $('#automationCoverChoice').value = 'off';
  await $('#autoPublishSelected').listeners.click();
  assert.equal(submitted.at(-1).url, '/api/pipeline');
  assert.equal(submitted.at(-1).body.automation_target, 'publish');
  assert.equal(submitted.at(-1).body.auto_publish, true);
  assert.equal(submitted.at(-1).body.cover_choice, 'off');
  assert.equal(submitted.at(-1).body.cover_cloud_authorized, false);
  $('#automationCoverChoice').value = 'cloud';
  $('#automationCoverCloudAuthorized').checked = false;
  state.selectedTasks.add('fixture');
  const beforeRejectedPublish = submitted.length;
  await $('#autoPublishSelected').listeners.click();
  assert.equal(submitted.length, beforeRejectedPublish);
  assert.match(messages.at(-1), /授权.*关闭封面/);
  $('#automationCoverCloudAuthorized').checked = true;
  await $('#autoPublishSelected').listeners.click();
  assert.equal(submitted.at(-1).body.cover_cloud_authorized, true);
  // A checked box cannot bypass a missing provider Key.
  state.dashboard.health.cover.api_key_configured = false;
  assert.throws(() => context.coverRequestValues(true), /Key/);
  context.updateCoverRequestControls();
  assert.equal($('#automationCoverCloudAuthorized').checked, true);
  assert.equal($('#automationCoverCloudAuthorized').disabled, false);
  state.dashboard.health.cover.api_key_configured = true;
  $('#manualCoverChoice').value = 'local';
  $('#manualCoverCloudAuthorized').checked = true;
  context.updateCoverRequestControls();
  assert.equal($('#manualCoverCloudAuthorized').checked, false);
  assert.equal(context.coverRequestValues(false).cover_cloud_authorized, false);
  assert.equal($('#automationCoverCloudAuthorized').checked, true);
  // An older backend may still accept an explicitly disabled cover.
  state.dashboard.health.cover.per_job_options = false;
  $('#automationCoverChoice').value = 'off';
  assert.equal(context.coverRequestValues(true).cover_enabled, false);
  console.log('Cover panel UI regressions passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
