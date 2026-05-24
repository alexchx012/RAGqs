const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function createClassList() {
  const classes = new Set();
  return {
    add: (...names) => names.forEach((name) => classes.add(name)),
    remove: (...names) => names.forEach((name) => classes.delete(name)),
    toggle: (name) => {
      if (classes.has(name)) {
        classes.delete(name);
        return false;
      }
      classes.add(name);
      return true;
    },
    contains: (name) => classes.has(name),
  };
}

function createElement() {
  return {
    children: [],
    className: '',
    dataset: {},
    disabled: false,
    innerHTML: '',
    scrollHeight: 0,
    scrollTop: 0,
    style: {},
    textContent: '',
    value: '',
    addEventListener() {},
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    querySelector() {
      return createElement();
    },
    classList: createClassList(),
  };
}

function createStorage() {
  const values = new Map();
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
  };
}

function loadApp(options = {}) {
  const appPath = path.join(__dirname, '..', 'static', 'app.js');
  const code = `${fs.readFileSync(appPath, 'utf8')}\nglobalThis.RAGAppForTest = RAGApp;`;
  const elements = new Map();
  const document = {
    addEventListener() {},
    createElement,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, createElement());
      return elements.get(id);
    },
    querySelectorAll() {
      return [];
    },
  };
  const sandbox = {
    console,
    document,
    fetch: options.fetch,
    localStorage: createStorage(),
    marked: undefined,
  };
  vm.runInNewContext(code, sandbox, { filename: appPath });
  return { App: sandbox.RAGAppForTest, localStorage: sandbox.localStorage };
}

function testNewChatDoesNotDuplicateLoadedHistory() {
  const { App, localStorage } = loadApp();
  const app = new App();

  app.sessionId = 'session-original';
  app.currentChatHistory = [
    { type: 'user', content: '什么是 RAG？' },
    { type: 'assistant', content: 'RAG 是检索增强生成。' },
  ];

  app.newChat();
  assert.equal(app.chatHistories.length, 1);

  const savedHistory = app.chatHistories[0];
  app.loadHistory(savedHistory);
  app.newChat();

  assert.equal(app.chatHistories.length, 1);
  assert.deepEqual(
    Array.from(app.chatHistories, (history) => history.id),
    ['session-original'],
  );

  const persisted = JSON.parse(localStorage.getItem('ragChatHistories'));
  assert.equal(persisted.length, 1);
  assert.equal(persisted[0].id, 'session-original');
}

function testLoadChatHistoriesDeduplicatesStoredSessions() {
  const { App, localStorage } = loadApp();
  localStorage.setItem(
    'ragChatHistories',
    JSON.stringify([
      {
        id: 'session-duplicate',
        title: '保留较新的历史',
        messages: [{ type: 'user', content: '新版' }],
      },
      {
        id: 'session-duplicate',
        title: '旧的重复历史',
        messages: [{ type: 'user', content: '旧版' }],
      },
    ]),
  );

  const app = new App();

  assert.equal(app.chatHistories.length, 1);
  assert.equal(app.chatHistories[0].title, '保留较新的历史');
}

async function testRefreshChatHistoriesLoadsBackendSummaries() {
  let requestedUrl = '';
  const { App } = loadApp({
    fetch: async (url) => {
      requestedUrl = url;
      return {
        ok: true,
        json: async () => ({
          code: 200,
          data: {
            sessions: [
              {
                id: 'server-session',
                title: '后端历史',
                messageCount: 2,
                updatedAt: '2026-05-24T12:00:00+00:00',
                lastMessage: '回答',
              },
            ],
          },
        }),
      };
    },
  });

  const app = new App();
  await app.historyRefreshPromise;

  assert.equal(requestedUrl, '/api/chat/sessions');
  assert.equal(app.chatHistories.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(app.chatHistories[0])), {
    id: 'server-session',
    title: '后端历史',
    messages: [],
    messageCount: 2,
    updatedAt: '2026-05-24T12:00:00+00:00',
    lastMessage: '回答',
    source: 'backend',
  });
}

async function testRefreshChatHistoriesSearchesBackendWithQuery() {
  const requestedUrls = [];
  const { App } = loadApp({
    fetch: async (url) => {
      requestedUrls.push(url);
      return {
        ok: true,
        json: async () => ({ code: 200, data: { sessions: [] } }),
      };
    },
  });
  const app = new App();
  await app.historyRefreshPromise;

  await app.refreshChatHistoriesFromBackend('alpha beta');

  assert.equal(requestedUrls.at(-1), '/api/chat/sessions?query=alpha%20beta');
}

async function testLoadBackendHistoryFetchesTranscriptBeforeRendering() {
  const requestedUrls = [];
  const { App } = loadApp({
    fetch: async (url) => {
      requestedUrls.push(url);
      if (url === '/api/chat/session/server-session') {
        return {
          ok: true,
          json: async () => ({
            session_id: 'server-session',
            message_count: 2,
            history: [
              { role: 'user', content: '问题' },
              { role: 'assistant', content: '答案' },
            ],
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({ code: 200, data: { sessions: [] } }),
      };
    },
  });
  const app = new App();
  await app.historyRefreshPromise;

  await app.loadHistory({
    id: 'server-session',
    title: '后端历史',
    messages: [],
    source: 'backend',
  });

  assert.ok(requestedUrls.includes('/api/chat/session/server-session'));
  assert.deepEqual(JSON.parse(JSON.stringify(app.currentChatHistory)), [
    { type: 'user', content: '问题' },
    { type: 'assistant', content: '答案' },
  ]);
}

async function run() {
  testNewChatDoesNotDuplicateLoadedHistory();
  testLoadChatHistoriesDeduplicatesStoredSessions();
  await testRefreshChatHistoriesLoadsBackendSummaries();
  await testRefreshChatHistoriesSearchesBackendWithQuery();
  await testLoadBackendHistoryFetchesTranscriptBeforeRendering();
  console.log('chat history tests passed');
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
