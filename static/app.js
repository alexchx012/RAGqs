// RAG 知识库问答前端
class RAGApp {
    constructor() {
        this.apiBaseUrl = '/api';
        this.currentMode = 'quick';
        this.sessionId = this.generateSessionId();
        this.isStreaming = false;
        this.currentChatHistory = [];
        this.chatHistories = this.loadChatHistories();
        this.selectedSpaceId = localStorage.getItem('ragSelectedSpaceId') || 'default';
        this.knowledgeSpaces = [];
        this.documents = [];
        this.indexJobs = [];
        this.retrievalAudits = [];

        this.initElements();
        this.bindEvents();
        this.initMarkdown();
        this.renderChatHistory();
        this.historyRefreshPromise = this.refreshChatHistoriesFromBackend();
    }

    initMarkdown() {
        if (typeof marked !== 'undefined') {
            marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
        }
    }

    renderMarkdown(content) {
        if (!content) return '';
        if (typeof marked === 'undefined') return this.escapeHtml(content);
        try { return marked.parse(content); } catch (e) { return this.escapeHtml(content); }
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    initElements() {
        this.messageInput = document.getElementById('messageInput');
        this.sendButton = document.getElementById('sendButton');
        this.chatMessages = document.getElementById('chatMessages');
        this.newChatBtn = document.getElementById('newChatBtn');
        this.toolsBtn = document.getElementById('toolsBtn');
        this.fileInput = document.getElementById('fileInput');
        this.modeSelectorBtn = document.getElementById('modeSelectorBtn');
        this.modeDropdown = document.getElementById('modeDropdown');
        this.currentModeText = document.getElementById('currentModeText');
        this.welcomeGreeting = document.getElementById('welcomeGreeting');
        this.chatHistoryList = document.getElementById('chatHistoryList');
        this.historySearchInput = document.getElementById('historySearchInput');
        this.spaceSelector = document.getElementById('spaceSelector');
        this.refreshSpacesBtn = document.getElementById('refreshSpacesBtn');
        this.createSpaceForm = document.getElementById('createSpaceForm');
        this.newSpaceIdInput = document.getElementById('newSpaceIdInput');
        this.newSpaceNameInput = document.getElementById('newSpaceNameInput');
        this.documentList = document.getElementById('documentList');
        this.refreshDocumentsBtn = document.getElementById('refreshDocumentsBtn');
        this.indexJobList = document.getElementById('indexJobList');
        this.refreshIndexJobsBtn = document.getElementById('refreshIndexJobsBtn');
        this.auditList = document.getElementById('auditList');
        this.refreshAuditsBtn = document.getElementById('refreshAuditsBtn');
        this.managementStatus = document.getElementById('managementStatus');
    }

    bindEvents() {
        this.sendButton.addEventListener('click', () => this.sendMessage());
        this.messageInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.sendMessage(); }
        });
        this.newChatBtn.addEventListener('click', () => this.newChat());
        this.toolsBtn.addEventListener('click', () => this.fileInput.click());
        this.fileInput.addEventListener('change', (e) => this.handleFileUpload(e));
        if (this.historySearchInput) {
            this.historySearchInput.addEventListener('input', () => {
                this.historyRefreshPromise = this.refreshChatHistoriesFromBackend(
                    this.historySearchInput.value,
                );
            });
        }
        if (this.spaceSelector) {
            this.spaceSelector.value = this.selectedSpaceId;
            this.spaceSelector.addEventListener('change', () => this.selectKnowledgeSpace(this.spaceSelector.value));
        }
        if (this.refreshSpacesBtn) {
            this.refreshSpacesBtn.addEventListener('click', () => this.refreshKnowledgeSpaces());
        }
        if (this.createSpaceForm) {
            this.createSpaceForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.createKnowledgeSpace();
            });
        }
        if (this.refreshDocumentsBtn) {
            this.refreshDocumentsBtn.addEventListener('click', () => this.refreshDocuments());
        }
        if (this.refreshIndexJobsBtn) {
            this.refreshIndexJobsBtn.addEventListener('click', () => this.refreshIndexJobs());
        }
        if (this.refreshAuditsBtn) {
            this.refreshAuditsBtn.addEventListener('click', () => this.refreshRetrievalAudits());
        }

        this.modeSelectorBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.modeDropdown.classList.toggle('active');
        });
        document.querySelectorAll('.dropdown-item').forEach(item => {
            item.addEventListener('click', () => {
                this.currentMode = item.dataset.mode;
                this.currentModeText.textContent = this.currentMode === 'quick' ? '快速' : '流式';
                document.querySelectorAll('.dropdown-item').forEach(i => i.classList.remove('active'));
                item.classList.add('active');
                this.modeDropdown.classList.remove('active');
            });
        });
        document.addEventListener('click', () => this.modeDropdown.classList.remove('active'));
    }

    getSelectedSpaceId() {
        return (this.spaceSelector?.value || this.selectedSpaceId || 'default').trim() || 'default';
    }

    async apiJson(path, options = {}) {
        const res = await fetch(`${this.apiBaseUrl}${path}`, options);
        const data = await res.json();
        if (!res.ok || (data.code && data.code >= 400)) {
            throw new Error(data.detail || data.message || '请求失败');
        }
        return data;
    }

    setManagementStatus(message, type = 'info') {
        if (!this.managementStatus) return;
        this.managementStatus.textContent = message || '';
        this.managementStatus.className = `management-status ${type}`;
    }

    async refreshKnowledgeSpaces() {
        const data = await this.apiJson('/knowledge-spaces');
        this.knowledgeSpaces = Array.isArray(data.data?.spaces) ? data.data.spaces : [];
        if (
            this.knowledgeSpaces.length > 0
            && !this.knowledgeSpaces.some(space => this.spaceIdOf(space) === this.selectedSpaceId)
        ) {
            this.selectedSpaceId = this.spaceIdOf(this.knowledgeSpaces[0]);
        }
        this.renderKnowledgeSpaces();
        await this.refreshDocuments();
        return this.knowledgeSpaces;
    }

    renderKnowledgeSpaces() {
        if (!this.spaceSelector) return;
        this.spaceSelector.innerHTML = '';
        const spaces = this.knowledgeSpaces.length > 0
            ? this.knowledgeSpaces
            : [{ space_id: this.selectedSpaceId, name: this.selectedSpaceId }];
        spaces.forEach(space => {
            const option = document.createElement('option');
            option.value = this.spaceIdOf(space);
            option.textContent = space.name || this.spaceIdOf(space);
            this.spaceSelector.appendChild(option);
        });
        this.spaceSelector.value = this.selectedSpaceId;
    }

    async selectKnowledgeSpace(spaceId) {
        this.selectedSpaceId = (spaceId || 'default').trim() || 'default';
        localStorage.setItem('ragSelectedSpaceId', this.selectedSpaceId);
        if (this.spaceSelector) this.spaceSelector.value = this.selectedSpaceId;
        await Promise.all([
            this.refreshDocuments(),
            this.refreshRetrievalAudits(),
        ]);
    }

    async createKnowledgeSpace() {
        const spaceId = (this.newSpaceIdInput?.value || '').trim();
        if (!spaceId) return;
        const name = (this.newSpaceNameInput?.value || '').trim() || spaceId;
        await this.apiJson('/knowledge-spaces', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ space_id: spaceId, name }),
        });
        this.selectedSpaceId = spaceId;
        if (this.newSpaceIdInput) this.newSpaceIdInput.value = '';
        if (this.newSpaceNameInput) this.newSpaceNameInput.value = '';
        await this.refreshKnowledgeSpaces();
        this.setManagementStatus(`知识空间 ${spaceId} 已创建`, 'success');
    }

    async refreshDocuments() {
        const spaceId = this.getSelectedSpaceId();
        const data = await this.apiJson(`/knowledge-spaces/${encodeURIComponent(spaceId)}/documents`);
        this.documents = Array.isArray(data.data?.documents) ? data.data.documents : [];
        this.renderDocuments();
        return this.documents;
    }

    renderDocuments() {
        if (!this.documentList) return;
        this.documentList.innerHTML = '';
        if (this.documents.length === 0) {
            this.documentList.textContent = '暂无文档';
            return;
        }
        this.documents.forEach(documentRecord => {
            const row = document.createElement('div');
            row.className = 'ops-row';
            const title = document.createElement('div');
            title.className = 'ops-row-main';
            title.textContent = `${documentRecord.file_name || documentRecord.document_id} · ${documentRecord.status || 'unknown'}`;
            const meta = document.createElement('div');
            meta.className = 'ops-row-meta';
            meta.textContent = `${documentRecord.indexed_chunks ?? 0}/${documentRecord.total_chunks ?? 0} chunks`;
            const actions = document.createElement('div');
            actions.className = 'ops-row-actions';
            const rebuild = document.createElement('button');
            rebuild.type = 'button';
            rebuild.textContent = '重建';
            rebuild.addEventListener('click', () => this.rebuildDocument(documentRecord.document_id));
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.textContent = '删除';
            remove.addEventListener('click', () => this.deleteDocument(documentRecord.document_id));
            actions.appendChild(rebuild);
            actions.appendChild(remove);
            row.appendChild(title);
            row.appendChild(meta);
            row.appendChild(actions);
            this.documentList.appendChild(row);
        });
    }

    async deleteDocument(documentId) {
        const spaceId = this.getSelectedSpaceId();
        await this.apiJson(
            `/knowledge-spaces/${encodeURIComponent(spaceId)}/documents/${encodeURIComponent(documentId)}`,
            { method: 'DELETE' },
        );
        await this.refreshDocuments();
        this.setManagementStatus('文档已删除', 'success');
    }

    async rebuildDocument(documentId) {
        const spaceId = this.getSelectedSpaceId();
        await this.apiJson(
            `/knowledge-spaces/${encodeURIComponent(spaceId)}/documents/${encodeURIComponent(documentId)}/rebuild`,
            { method: 'POST' },
        );
        await this.refreshDocuments();
        this.setManagementStatus('重建任务已提交', 'success');
    }

    async refreshIndexJobs() {
        const data = await this.apiJson('/index-jobs');
        this.indexJobs = Array.isArray(data.data?.jobs) ? data.data.jobs : [];
        this.renderIndexJobs();
        return this.indexJobs;
    }

    renderIndexJobs() {
        if (!this.indexJobList) return;
        this.indexJobList.innerHTML = '';
        if (this.indexJobs.length === 0) {
            this.indexJobList.textContent = '暂无索引任务';
            return;
        }
        this.indexJobs.forEach(job => {
            const row = document.createElement('div');
            row.className = 'ops-row';
            const title = document.createElement('div');
            title.className = 'ops-row-main';
            title.textContent = `${job.job_id} · ${job.status}`;
            const meta = document.createElement('div');
            meta.className = 'ops-row-meta';
            meta.textContent = job.document_id || job.source_path || '';
            const actions = document.createElement('div');
            actions.className = 'ops-row-actions';
            const retry = document.createElement('button');
            retry.type = 'button';
            retry.textContent = '重试';
            retry.addEventListener('click', () => this.retryIndexJob(job.job_id));
            actions.appendChild(retry);
            row.appendChild(title);
            row.appendChild(meta);
            row.appendChild(actions);
            this.indexJobList.appendChild(row);
        });
    }

    async retryIndexJob(jobId) {
        await this.apiJson(`/index-jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST' });
        await this.refreshIndexJobs();
        this.setManagementStatus('索引任务已重试', 'success');
    }

    async refreshRetrievalAudits() {
        const spaceId = this.getSelectedSpaceId();
        const data = await this.apiJson(`/chat/audits?space_id=${encodeURIComponent(spaceId)}`);
        this.retrievalAudits = Array.isArray(data.data?.audits) ? data.data.audits : [];
        this.renderRetrievalAudits();
        return this.retrievalAudits;
    }

    renderRetrievalAudits() {
        if (!this.auditList) return;
        this.auditList.innerHTML = '';
        if (this.retrievalAudits.length === 0) {
            this.auditList.textContent = '暂无审计记录';
            return;
        }
        this.retrievalAudits.forEach(audit => {
            const row = document.createElement('div');
            row.className = 'ops-row';
            const title = document.createElement('div');
            title.className = 'ops-row-main';
            title.textContent = audit.question || audit.traceId || audit.id || 'audit';
            const meta = document.createElement('div');
            meta.className = 'ops-row-meta';
            meta.textContent = `${(audit.sources || []).length} sources · ${audit.createdAt || ''}`;
            row.appendChild(title);
            row.appendChild(meta);
            this.auditList.appendChild(row);
        });
    }

    spaceIdOf(space) {
        return space?.space_id || space?.spaceId || space?.id || 'default';
    }

    generateSessionId() {
        return 'session_' + Math.random().toString(36).substr(2, 9) + '_' + Date.now();
    }

    newChat() {
        if (this.currentChatHistory.length > 0) this.saveCurrentChat();
        this.currentChatHistory = [];
        this.chatMessages.innerHTML = '';
        this.sessionId = this.generateSessionId();
        if (this.welcomeGreeting) this.welcomeGreeting.style.display = '';
        this.renderChatHistory();
    }

    async sendMessage() {
        const message = this.messageInput.value.trim();
        if (!message || this.isStreaming) return;
        this.addMessage('user', message);
        this.messageInput.value = '';
        this.isStreaming = true;
        this.sendButton.disabled = true;
        try {
            if (this.currentMode === 'quick') await this.sendQuick(message);
            else await this.sendStream(message);
        } catch (e) {
            this.addMessage('assistant', '错误: ' + e.message);
        } finally {
            this.isStreaming = false;
            this.sendButton.disabled = false;
        }
    }

    async sendQuick(message) {
        const res = await fetch(`${this.apiBaseUrl}/chat`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                Id: this.sessionId,
                Question: message,
                spaceId: this.getSelectedSpaceId(),
            }),
        });
        const data = await res.json();
        if (data.code === 200 && data.data?.success) {
            this.addMessage('assistant', data.data.answer || '（无回复）');
        } else {
            throw new Error(data.data?.errorMessage || data.message || '请求失败');
        }
    }

    async sendStream(message) {
        const res = await fetch(`${this.apiBaseUrl}/chat_stream`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                Id: this.sessionId,
                Question: message,
                spaceId: this.getSelectedSpaceId(),
            }),
        });
        const el = this.addMessage('assistant', '', true);
        const contentEl = el.querySelector('.message-content');
        let fullResponse = '';
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.startsWith('data:')) continue;
                const raw = line.substring(5).trim();
                if (raw === '[DONE]') break;
                try {
                    const msg = JSON.parse(raw);
                    if (msg.type === 'content') {
                        fullResponse += msg.data || '';
                        contentEl.innerHTML = this.renderMarkdown(fullResponse);
                    } else if (msg.type === 'done') break;
                } catch (e) { /* skip */ }
            }
        }
        reader.releaseLock();
        if (fullResponse) {
            contentEl.innerHTML = this.renderMarkdown(fullResponse);
            this.currentChatHistory.push({ type: 'assistant', content: fullResponse });
        }
        this.scrollToBottom();
    }

    addMessage(type, content, isStreaming = false) {
        if (this.welcomeGreeting) this.welcomeGreeting.style.display = 'none';
        const div = document.createElement('div');
        div.className = `message ${type} ${type}-message`;
        const contentDiv = document.createElement('div');
        contentDiv.className = 'message-content';
        contentDiv.innerHTML = type === 'user' ? this.escapeHtml(content) : this.renderMarkdown(content);
        div.appendChild(contentDiv);
        this.chatMessages.appendChild(div);
        if (!isStreaming) this.currentChatHistory.push({ type, content });
        this.scrollToBottom();
        return div;
    }

    scrollToBottom() {
        this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
    }

    async handleFileUpload(e) {
        const file = e.target.files[0];
        if (!file) return;
        const formData = new FormData();
        formData.append('file', file);
        try {
            const res = await fetch(
                `${this.apiBaseUrl}/upload?space_id=${encodeURIComponent(this.getSelectedSpaceId())}`,
                { method: 'POST', body: formData },
            );
            const data = await res.json();
            if (data.code === 200) {
                this.addMessage('assistant', `✅ 文件 "${file.name}" 上传成功，已建立向量索引。`);
                await Promise.all([this.refreshDocuments(), this.refreshIndexJobs()]);
            } else {
                this.addMessage('assistant', `❌ 上传失败: ${data.detail || data.message}`);
            }
        } catch (err) {
            this.addMessage('assistant', `❌ 上传出错: ${err.message}`);
        }
        this.fileInput.value = '';
    }

    saveCurrentChat() {
        if (this.currentChatHistory.length === 0) return;
        const first = this.currentChatHistory.find(m => m.type === 'user');
        const title = first ? first.content.substring(0, 30) : '新对话';
        const currentChat = { id: this.sessionId, title, messages: [...this.currentChatHistory] };
        this.chatHistories = this.chatHistories.filter(h => h.id !== this.sessionId);
        this.chatHistories.unshift(currentChat);
        if (this.chatHistories.length > 30) this.chatHistories = this.chatHistories.slice(0, 30);
        this.persistChatHistories();
        this.renderChatHistory();
    }

    loadChatHistories() {
        try {
            const histories = JSON.parse(localStorage.getItem('ragChatHistories') || '[]');
            const normalized = this.normalizeChatHistories(histories);
            if (Array.isArray(histories) && normalized.length !== histories.length) {
                localStorage.setItem('ragChatHistories', JSON.stringify(normalized));
            }
            return normalized;
        } catch {
            return [];
        }
    }

    normalizeChatHistories(histories) {
        if (!Array.isArray(histories)) return [];
        const seen = new Set();
        const normalized = [];
        histories.forEach(h => {
            if (!h || !h.id || seen.has(h.id)) return;
            seen.add(h.id);
            normalized.push({
                ...h,
                title: h.title || '新对话',
                messages: Array.isArray(h.messages) ? h.messages : [],
            });
        });
        return normalized;
    }

    async refreshChatHistoriesFromBackend(query = '') {
        if (typeof fetch !== 'function') return this.chatHistories;
        const trimmedQuery = (query || '').trim();
        const search = trimmedQuery ? `?query=${encodeURIComponent(trimmedQuery)}` : '';

        try {
            const res = await fetch(`${this.apiBaseUrl}/chat/sessions${search}`);
            if (!res.ok) return this.chatHistories;
            const data = await res.json();
            const sessions = data?.data?.sessions;
            if (!Array.isArray(sessions)) return this.chatHistories;

            const backendHistories = sessions
                .map(session => this.normalizeBackendHistory(session))
                .filter(Boolean);
            const localHistories = trimmedQuery ? [] : this.loadChatHistories();
            this.chatHistories = this.normalizeChatHistories([...backendHistories, ...localHistories]);
            this.renderChatHistory();
            return this.chatHistories;
        } catch {
            return this.chatHistories;
        }
    }

    normalizeBackendHistory(session) {
        const id = session?.id || session?.session_id;
        if (!id) return null;
        return {
            id,
            title: session.title || '新对话',
            messages: [],
            messageCount: session.messageCount ?? session.message_count ?? 0,
            updatedAt: session.updatedAt || session.updated_at || '',
            lastMessage: session.lastMessage || session.last_message || '',
            source: 'backend',
        };
    }

    persistChatHistories() {
        const persisted = this.chatHistories.filter(
            h => Array.isArray(h.messages) && h.messages.length > 0,
        );
        localStorage.setItem('ragChatHistories', JSON.stringify(persisted));
    }

    renderChatHistory() {
        this.chatHistoryList.innerHTML = '';
        this.chatHistories.forEach(h => {
            const div = document.createElement('div');
            div.className = 'history-item';
            div.textContent = h.title;
            div.addEventListener('click', () => this.loadHistory(h));
            this.chatHistoryList.appendChild(div);
        });
    }

    loadHistory(history) {
        if (Array.isArray(history.messages) && history.messages.length > 0) {
            return this.renderLoadedHistory(history);
        }
        return this.loadHistoryMessages(history).then(loadedHistory => (
            this.renderLoadedHistory(loadedHistory)
        ));
    }

    renderLoadedHistory(loadedHistory) {
        this.sessionId = loadedHistory.id;
        this.currentChatHistory = [...loadedHistory.messages];
        this.chatMessages.innerHTML = '';
        if (this.welcomeGreeting) this.welcomeGreeting.style.display = 'none';
        loadedHistory.messages.forEach(m => {
            const div = document.createElement('div');
            div.className = `message ${m.type} ${m.type}-message`;
            const c = document.createElement('div');
            c.className = 'message-content';
            c.innerHTML = m.type === 'user' ? this.escapeHtml(m.content) : this.renderMarkdown(m.content);
            div.appendChild(c);
            this.chatMessages.appendChild(div);
        });
        this.scrollToBottom();
        return loadedHistory;
    }

    async loadHistoryMessages(history) {
        if (Array.isArray(history.messages) && history.messages.length > 0) return history;
        if (history.source !== 'backend' || typeof fetch !== 'function') {
            return { ...history, messages: history.messages || [] };
        }

        try {
            const res = await fetch(`${this.apiBaseUrl}/chat/session/${encodeURIComponent(history.id)}`);
            if (!res.ok) return { ...history, messages: [] };
            const data = await res.json();
            const rawMessages = Array.isArray(data.history) ? data.history : [];
            const messages = rawMessages
                .map(message => this.normalizeHistoryMessage(message))
                .filter(Boolean);
            const loadedHistory = { ...history, messages };
            this.chatHistories = this.chatHistories.map(h => (
                h.id === loadedHistory.id ? loadedHistory : h
            ));
            this.persistChatHistories();
            return loadedHistory;
        } catch {
            return { ...history, messages: [] };
        }
    }

    normalizeHistoryMessage(message) {
        const type = message?.type || message?.role;
        const content = message?.content;
        if (!type || typeof content !== 'string') return null;
        return {
            type: type === 'user' ? 'user' : 'assistant',
            content,
        };
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const app = new RAGApp();
    app.refreshKnowledgeSpaces().catch(() => app.renderKnowledgeSpaces());
    app.refreshIndexJobs().catch(() => {});
    app.refreshRetrievalAudits().catch(() => {});
});
