// RAG 知识库问答前端
class RAGApp {
    constructor() {
        this.apiBaseUrl = '/api';
        this.currentMode = 'quick';
        this.sessionId = this.generateSessionId();
        this.isStreaming = false;
        this.currentChatHistory = [];
        this.chatHistories = this.loadChatHistories();

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
            body: JSON.stringify({ Id: this.sessionId, Question: message }),
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
            body: JSON.stringify({ Id: this.sessionId, Question: message }),
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
        div.className = `message ${type}-message`;
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
            const res = await fetch(`${this.apiBaseUrl}/upload`, { method: 'POST', body: formData });
            const data = await res.json();
            if (data.code === 200) {
                this.addMessage('assistant', `✅ 文件 "${file.name}" 上传成功，已建立向量索引。`);
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
            div.className = `message ${m.type}-message`;
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

document.addEventListener('DOMContentLoaded', () => new RAGApp());
