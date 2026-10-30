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
    drawHistory: () => {}, showLatestMessage: () => {}, queueMicrotask,
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


test('a follow-up keeps one sidebar entry with the original conversation title', () => {
  const list = { innerHTML: '', querySelectorAll: () => [] };
  const ctx = vm.createContext({
    $: (selector) => selector === '#history-filter' ? {value: ''} : selector.includes('aria-current') ? null : list,
    STATUS: {answered: ['Answered', 'ok']}, esc: (value) => value,
    activeThreadId: 10, editingThread: null,
    historyItems: [
      {id: 11, thread_id: 10, title: 'How to submit a ticket?', question: "So what's the first step?", response: {status: 'answered'}, created_at: '2026-10-30T11:15:00Z'},
      {id: 10, thread_id: 10, title: 'How to submit a ticket?', question: 'How to submit a ticket?', response: {status: 'answered'}, created_at: '2026-10-30T10:54:00Z'},
    ],
  });
  const draw = html.slice(html.indexOf('function drawHistory(current)'), html.indexOf('$("#history-filter").addEventListener'));
  vm.runInContext(draw + '\ndrawHistory();', ctx);
  assert.equal((list.innerHTML.match(/data-h=/g) || []).length, 1);
  assert.match(list.innerHTML, /How to submit a ticket/);
  assert.doesNotMatch(list.innerHTML, /So what's the first step/);
  assert.match(list.innerHTML, /aria-current="true"/);
});


test('each answer renders inside its own message instead of overwriting the first', () => {
  const first = {answer: {innerHTML: ''}, querySelector() { return this.answer; }};
  const second = {answer: {innerHTML: ''}, querySelector() { return this.answer; }};
  const context = vm.createContext({document: first, first, second});
  const selector = html.match(/const \$ = .*?;/)[0];
  vm.runInContext(selector + `
    $('.ans-body', first).innerHTML = 'Open the My Account menu';
    $('.ans-body', second).innerHTML = 'Next, choose Create New Ticket';
  `, context);
  assert.equal(first.answer.innerHTML, 'Open the My Account menu');
  assert.equal(second.answer.innerHTML, 'Next, choose Create New Ticket');
});


test('opening a conversation scrolls to the latest message after all turns render', async () => {
  const frames = [], rendered = [], viewport = {scrollHeight: 0, scrollTo(options) { this.position = options.top; }};
  const ctx = vm.createContext({
    threadRevision: 0, busy: false, activeThreadId: null, visibleHistoryIds: [],
    requestAnimationFrame: (fn) => frames.push(fn),
    $: (selector) => selector === '#chat-scroll' ? viewport : {},
    api: async () => ({items: [{id: 2, question: 'Follow-up', response: {}}, {id: 1, question: 'First', response: {}}], next_before: null}),
    rememberThread: () => {}, showTab: () => {}, drawHistory: () => {},
    newTurn: () => { viewport.scrollHeight += 400; return {}; },
    renderAnswer: (_, question) => rendered.push(question),
  });
  const code = html.slice(html.indexOf('function showLatestMessage()'), html.indexOf('// Chat history:'));
  await vm.runInContext(code + '\nopenConversation({id: 1});', ctx);
  assert.deepEqual(rendered, ['First', 'Follow-up']);
  frames.forEach((fn) => fn());
  assert.equal(viewport.position, 800);
});
