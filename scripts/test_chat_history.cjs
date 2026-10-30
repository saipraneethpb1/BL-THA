const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const html = fs.readFileSync('task1-knowledge-brain/kb/ui/index.html', 'utf8');
const code = html.slice(html.indexOf('let historyItems = [];'), html.indexOf('function drawHistory(current)'));
function harness(saved, pages) {
  const elements = new Map();
  const rendered = [];
  const context = vm.createContext({
    localStorage: { getItem: () => saved },
    $: (key) => { if (!elements.has(key)) elements.set(key, {}); return elements.get(key); },
    api: async () => { const page = pages.shift(); if (page instanceof Error) throw page; return page; },
    renderAnswer: (_, question) => rendered.push(question), newTurn: () => ({}),
    drawHistory: () => {}, queueMicrotask,
  });
  vm.runInContext('let visibleHistoryIds = [], threadRevision = 0, busy = false;\n' + code, context);
  return { context, elements, rendered, load: () => vm.runInContext('loadHistory(undefined, false, true)', context) };
}
const item = (id) => ({ id, question: `Question ${id}`, response: {} });
test('restores all visible turns across history pages in conversation order', async () => {
  const h = harness('[1,3]', [{ items: [item(3), item(2)], next_before: 2 }, { items: [item(1)], next_before: null }]);
  await h.load();
  assert.deepEqual(h.rendered, ['Question 1', 'Question 3']);
});
test('an explicitly new question stays empty on refresh', async () => {
  const h = harness('[]', [{ items: [item(3)], next_before: null }]);
  await h.load(); assert.deepEqual(h.rendered, []);
});
test('first visit reopens most recent saved answer', async () => {
  const h = harness(null, [{ items: [item(3)], next_before: null }]);
  await h.load(); assert.deepEqual(h.rendered, ['Question 3']);
});
test('saved IDs from another account cannot render answers', async () => {
  const h = harness('[99]', [{ items: [item(3)], next_before: null }]);
  await h.load(); assert.deepEqual(h.rendered, []);
});
test('history failure exposes retry without claiming history is empty', async () => {
  const h = harness(null, [new Error('offline')]);
  await h.load(); assert.equal(h.elements.get('#history-more').hidden, false);
  assert.match(h.elements.get('#history-note').textContent, /Couldn't load/);
});
// Parse every inline script to catch syntax errors outside the exercised section.
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
