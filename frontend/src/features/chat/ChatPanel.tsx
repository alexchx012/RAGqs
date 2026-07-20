import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useChat } from './ChatContext';
import { useChatStream } from './useChatStream';
import { useChatQuick } from './useChatQuick';
import { renderMarkdown, escapeHtml } from '../../markdown/renderMarkdown';

interface ChatPanelProps {
  spaceId: string;
  uploadSlot?: React.ReactNode;
  disabled?: boolean;
}

export default function ChatPanel({ spaceId, uploadSlot, disabled = false }: ChatPanelProps) {
  const {
    currentChatHistory,
    isStreaming,
    mode,
    setMode,
    addMessage,
  } = useChat();

  const { sendStream } = useChatStream();
  const { sendQuick } = useChatQuick();

  const [inputValue, setInputValue] = useState('');
  const [showModeDropdown, setShowModeDropdown] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [currentChatHistory, scrollToBottom]);

  const handleSend = useCallback(async () => {
    const text = inputValue.trim();
    if (!text || isStreaming || disabled) return;

    addMessage({ type: 'user', content: text });
    setInputValue('');

    if (mode === 'quick') {
      await sendQuick(text, spaceId);
    } else {
      await sendStream(text, spaceId, (errorMsg) => {
        addMessage({
          type: 'assistant',
          content: `错误: ${errorMsg}`,
        });
      });
    }
  }, [inputValue, isStreaming, disabled, mode, spaceId, addMessage, sendQuick, sendStream]);

  const handleKeyPress = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const modeLabel = mode === 'quick' ? '快速' : '流式';

  return (
    <div className="chat-container">
      <div className="chat-messages" id="chatMessages">
        {currentChatHistory.length === 0 && (
          <div className="welcome-greeting" style={{ opacity: 1, visibility: 'visible', height: 'auto', padding: '20px' }}>
            <p>你好！我是知识库问答助手</p>
          </div>
        )}
        {currentChatHistory.map((msg, i) => (
          <div key={i} className={`message ${msg.type} ${msg.type}-message`}>
            <div
              className="message-content"
              dangerouslySetInnerHTML={{
                __html: msg.type === 'user' ? escapeHtml(msg.content) : renderMarkdown(msg.content),
              }}
            />
            {msg.type === 'assistant' && msg.answerMode === 'no_context' && (
              <div className="answer-mode-hint answer-mode-hint-soft" data-testid="answer-mode-no-context">
                未在知识库中找到相关内容
              </div>
            )}
            {msg.type === 'assistant' &&
              msg.answerMode === 'direct' &&
              msg.usedToolsWithoutKnowledgeBase && (
                <div className="answer-mode-hint answer-mode-hint-warning" data-testid="answer-mode-warning">
                  本次回答未使用知识库内容
                </div>
              )}
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input-container">
        <div className="input-group-wrapper">
          <div className="input-wrapper">
            <input
              type="text"
              className="message-input"
              placeholder="输入你的问题..."
              maxLength={1000}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={handleKeyPress}
              disabled={isStreaming || disabled}
            />
            <div className="input-bottom-bar">
              <div className="tools-btn-wrapper">
                {uploadSlot}
              </div>
              <div className="right-actions">
                <div className="mode-selector-wrapper">
                  <button
                    className="mode-selector-btn"
                    onClick={() => setShowModeDropdown(!showModeDropdown)}
                  >
                    <span>{modeLabel}</span>
                    <svg className="dropdown-arrow" viewBox="0 0 24 24" fill="none">
                      <path d="M6 9L12 15L18 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  {showModeDropdown && (
                    <div className="mode-dropdown active">
                      <div
                        className={`dropdown-item ${mode === 'quick' ? 'active' : ''}`}
                        onClick={() => { setMode('quick'); setShowModeDropdown(false); }}
                      >
                        <span>快速</span>
                      </div>
                      <div
                        className={`dropdown-item ${mode === 'stream' ? 'active' : ''}`}
                        onClick={() => { setMode('stream'); setShowModeDropdown(false); }}
                      >
                        <span>流式</span>
                      </div>
                    </div>
                  )}
                </div>
                <button
                  className="send-btn-circle"
                  onClick={handleSend}
                  disabled={isStreaming || disabled || !inputValue.trim()}
                  title="发送"
                >
                  <svg viewBox="0 0 24 24" fill="none">
                    <path d="M22 2L11 13M22 2L15 22L11 13M22 2L2 9L11 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
