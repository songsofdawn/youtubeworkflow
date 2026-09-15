// Offline interaction tests for taxonomy saves and safe diagnostics rendering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../src/control_panel/static');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const upgrade = fs.readFileSync(path.join(root, 'discovery_upgrade.js'), 'utf8');
const elements = new Map();
function element() {
  return { innerHTML: '', textContent: '', value: '', disabled: false,
    events: {}, open: false,
    addEventListener(event, callback) { this.events[event] = callback; },
    querySelectorAll() { return []; },
    querySelector(selector) { return get(selector); },
    showModal() { this.open = true; }, close() { this.open = false; },
  };
}
function get(selector) {
  if (!elements.has(selector)) elements.set(selector, element());
  return elements.get(selector);
}
const entities = ['civilization', 'AI agents'];
let saved;
let failSave = false;
let reloads = 0;
const alerts = [];
const document = {
  createElement: element,
  body: { appendChild(node) { elements.set(`#${node.id}`, node); } },
  querySelector(selector) { return elements.get(selector) || null; },
  querySelectorAll() { return Array.from({ length: 20 }); },
};
const context = vm.createContext({ document,
  qs: (selector, node = document) => node.querySelector(selector),
  window: {
    alert(message) { alerts.push(message); },
    location: { reload() { reloads++; } },
  },
  requestJson: async (url, options) => {
    if (options?.method === 'POST') {
      saved = JSON.parse(options.body);
      if (failSave) throw new Error('unknown format');
      return saved;
    }
    return { packs: [{ id: 'custom', label: 'Custom', description: 'Description',
      entities, formats: ['simulation'], positive_traits: ['visible_result'], negative_intents: [], default_selected: true }] };
  },
});
vm.runInContext(app.slice(app.indexOf('function escapeHtml('), app.indexOf('async function api(')), context);
vm.runInContext(app.slice(app.indexOf('function discoveryQueryDiagnosticsMarkup('), app.indexOf('function renderSearchResults(')), context);
vm.runInContext(upgrade.slice(upgrade.indexOf('  let editorPacks'), upgrade.indexOf('  function updateDiscoveryQuotaEstimate(')), context);
vm.runInContext(upgrade.slice(upgrade.indexOf('  function updateDiscoveryQuotaEstimate('), upgrade.indexOf('  function setupDiscoveryQuotaEstimate(')), context);

(async () => {
  await context.openEditor();
  const dialog = get('#discoveryPackEditorDialog');
  assert.equal(dialog.open, true);
  assert.match(get('#discoveryPackEditorList').innerHTML, /civilization/);
  await get('#discoveryPackEditorForm').events.submit({ preventDefault() {} });
  assert.deepEqual(saved.packs[0].entities, entities);
  assert.deepEqual(saved.packs[0].formats, ['simulation']);
  assert.ok(!('query' in saved.packs[0]));
  assert.equal(dialog.open, false);
  assert.equal(get('#saveDiscoveryPacks').disabled, false);
  assert.equal(reloads, 1);
  assert.equal(alerts.length, 0);

  await context.openEditor();
  failSave = true;
  await get('#discoveryPackEditorForm').events.submit({ preventDefault() {} });
  assert.equal(dialog.open, true, 'validation errors must keep the editor open');
  assert.match(alerts[0], /unknown format/);
  assert.equal(get('#saveDiscoveryPacks').disabled, false);

  assert.equal(context.discoveryQueryDiagnosticsMarkup(undefined), '', 'old results need no diagnostics');
  assert.equal(context.discoveryQueryDiagnosticsMarkup([]), '');
  const html = context.discoveryQueryDiagnosticsMarkup([{ query: '<img src=x onerror=alert(1)>',
    orders: ['relevance', 'viewCount'], calls: 2, unique_count: 4,
    eligible_count: 3, new_eligible_count: 2, channel_count: 2, selected_count: 1 }]);
  assert.ok(!html.includes('<img'));
  assert.match(html, /&lt;img/);
  assert.match(html, /相关性 \/ 热门/);
  assert.match(html, /各行不可相加/);

  get('#discoveryMaxSearchRequests').value = '96';
  get('#discoveryQuotaEstimate');
  context.updateDiscoveryQuotaEstimate();
  assert.match(get('#discoveryQuotaEstimate').textContent, /全局最多 96 次/);
  assert.ok(!get('#discoveryQuotaEstimate').textContent.includes('120 次'));
  const nextHtml = context.discoveryQueryDiagnosticsMarkup([{run_id: 'test', query: '<script>', entity: 'mods', format: 'simulation', order: 'date', status: 'complete'}]);
  assert.ok(!nextHtml.includes('<script>'));
  assert.match(nextHtml, /动态查询绩效/);
  console.log('Discovery Next UI: taxonomy saves, validation, legacy results, escaping and global budget passed.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
