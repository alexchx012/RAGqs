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
    files: [],
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
    FormData: class {
      constructor() {
        this.values = [];
      }

      append(key, value) {
        this.values.push([key, value]);
      }
    },
    localStorage: createStorage(),
    marked: undefined,
  };
  vm.runInNewContext(code, sandbox, { filename: appPath });
  return { App: sandbox.RAGAppForTest, elements, localStorage: sandbox.localStorage };
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

async function testChatAndUploadSendSelectedKnowledgeSpace() {
  const requests = [];
  const { App } = loadApp({
    fetch: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === '/api/chat') {
        return {
          ok: true,
          json: async () => ({
            code: 200,
            data: {
              success: true,
              answer: 'answer',
              sources: [],
              retrieval: { debug: {} },
            },
          }),
        };
      }
      if (String(url).startsWith('/api/upload')) {
        return {
          ok: true,
          json: async () => ({ code: 200, data: { filename: 'guide.md' } }),
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
  app.selectedSpaceId = 'finance';
  app.spaceSelector.value = 'finance';

  await app.sendQuick('policy');
  app.fileInput.files = [{ name: 'guide.md' }];
  await app.handleFileUpload({ target: app.fileInput });

  const chatBody = JSON.parse(requests.find((request) => request.url === '/api/chat').options.body);
  assert.equal(chatBody.spaceId, 'finance');
  assert.equal(
    requests.find((request) => String(request.url).startsWith('/api/upload')).url,
    '/api/upload?space_id=finance',
  );
}

async function testManagementFlowsUseBackendSpaceAndLifecycleApis() {
  const requested = [];
  const { App } = loadApp({
    fetch: async (url, options = {}) => {
      requested.push({ url, method: options.method || 'GET' });
      if (url === '/api/knowledge-spaces') {
        return {
          ok: true,
          json: async () => ({
            code: 200,
            data: { spaces: [{ space_id: 'finance', name: 'Finance' }] },
          }),
        };
      }
      if (url === '/api/knowledge-spaces/finance/documents') {
        return {
          ok: true,
          json: async () => ({
            code: 200,
            data: {
              documents: [
                {
                  document_id: 'doc-1',
                  file_name: 'guide.md',
                  status: 'indexed',
                  total_chunks: 2,
                  indexed_chunks: 2,
                },
              ],
            },
          }),
        };
      }
      if (url === '/api/index-jobs') {
        return {
          ok: true,
          json: async () => ({
            code: 200,
            data: { jobs: [{ job_id: 'job-1', status: 'failed' }] },
          }),
        };
      }
      if (url === '/api/chat/audits?space_id=finance') {
        return {
          ok: true,
          json: async () => ({
            code: 200,
            data: { audits: [{ id: 'audit-1', question: 'q', answer: 'a' }] },
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({ code: 200, data: {} }),
      };
    },
  });
  const app = new App();
  await app.refreshKnowledgeSpaces();
  await app.refreshDocuments();
  await app.deleteDocument('doc-1');
  await app.rebuildDocument('doc-1');
  await app.refreshIndexJobs();
  await app.retryIndexJob('job-1');
  await app.refreshRetrievalAudits();

  assert.deepEqual(
    requested.map((request) => `${request.method} ${request.url}`),
    [
      'GET /api/chat/sessions',
      'GET /api/knowledge-spaces',
      'GET /api/knowledge-spaces/finance/documents',
      'GET /api/knowledge-spaces/finance/documents',
      'DELETE /api/knowledge-spaces/finance/documents/doc-1',
      'GET /api/knowledge-spaces/finance/documents',
      'POST /api/knowledge-spaces/finance/documents/doc-1/rebuild',
      'GET /api/knowledge-spaces/finance/documents',
      'GET /api/index-jobs',
      'POST /api/index-jobs/job-1/retry',
      'GET /api/index-jobs',
      'GET /api/chat/audits?space_id=finance',
    ],
  );
}

function testUploadAcceptsBackendAllowedTextFormats() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

  assert.match(html, /accept="\.txt,\.md,\.markdown,\.csv,\.html,\.htm,\.json"/);
}

function testRenderedMessagesKeepRoleClassesUsedByCss() {
  const { App, elements } = loadApp();
  const app = new App();

  app.addMessage('user', 'hello');
  app.addMessage('assistant', 'world');

  const [userMessage, assistantMessage] = elements.get('chatMessages').children;
  const userClasses = new Set(userMessage.className.split(/\s+/));
  const assistantClasses = new Set(assistantMessage.className.split(/\s+/));
  assert.ok(userClasses.has('message'));
  assert.ok(userClasses.has('user'));
  assert.ok(userClasses.has('user-message'));
  assert.ok(assistantClasses.has('message'));
  assert.ok(assistantClasses.has('assistant'));
  assert.ok(assistantClasses.has('assistant-message'));
}

function testModeDropdownActiveClassHasVisibleCssRule() {
  const css = fs.readFileSync(path.join(__dirname, '..', 'static', 'styles.css'), 'utf8');

  assert.match(css, /\.mode-dropdown\.active\s*\{/);
}

async function run() {
  testNewChatDoesNotDuplicateLoadedHistory();
  testLoadChatHistoriesDeduplicatesStoredSessions();
  await testRefreshChatHistoriesLoadsBackendSummaries();
  await testRefreshChatHistoriesSearchesBackendWithQuery();
  await testLoadBackendHistoryFetchesTranscriptBeforeRendering();
  await testChatAndUploadSendSelectedKnowledgeSpace();
  await testManagementFlowsUseBackendSpaceAndLifecycleApis();
  testUploadAcceptsBackendAllowedTextFormats();
  testRenderedMessagesKeepRoleClassesUsedByCss();
  testModeDropdownActiveClassHasVisibleCssRule();
  console.log('chat history tests passed');
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
