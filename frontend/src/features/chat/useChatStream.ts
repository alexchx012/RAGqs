import { useRef, useCallback, useEffect } from 'react';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { useChat } from './ChatContext';

interface StreamMessage {
  type: 'content' | 'done' | 'error' | 'answer_mode';
  data?: string | { mode: 'direct' | 'grounded' | 'no_context'; usedToolsWithoutKnowledgeBase: boolean };
}

export function useChatStream() {
  const { sessionId, addMessage, replaceLastMessage, setStreaming, registerStreamAbort } = useChat();
  const abortRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

  useEffect(() => {
    registerStreamAbort(abort);
    return () => {
      abort();
      registerStreamAbort(null);
    };
  }, [registerStreamAbort, abort]);

  const sendStream = useCallback(
    async (
      message: string,
      spaceId: string,
      onError: (msg: string) => void,
    ): Promise<void> => {
      setStreaming(true);
      const controller = new AbortController();
      abortRef.current = controller;

      let fullResponse = '';
      let hasProgressiveMsg = false;
      let answerMode: 'direct' | 'grounded' | 'no_context' | undefined;
      let usedToolsWithoutKnowledgeBase: boolean | undefined;

      try {
        await fetchEventSource('/api/chat_stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            Id: sessionId,
            Question: message,
            spaceId,
          }),
          signal: controller.signal,
          async onopen(response) {
            if (!response.ok) {
              const text = await response.text();
              let detail = `HTTP ${response.status}`;
              try {
                const parsed = JSON.parse(text);
                detail = parsed.detail || parsed.message || detail;
              } catch {
                /* use status text */
              }
              throw new Error(detail);
            }
          },
          onmessage(ev) {
            try {
              const msg: StreamMessage = JSON.parse(ev.data);
              if (msg.type === 'content') {
                fullResponse += (msg.data as string) || '';
                if (!hasProgressiveMsg) {
                  addMessage({ type: 'assistant', content: fullResponse });
                  hasProgressiveMsg = true;
                } else {
                  replaceLastMessage({ type: 'assistant', content: fullResponse });
                }
              } else if (msg.type === 'done') {
                return;
              } else if (msg.type === 'answer_mode') {
                const modeData = msg.data as {
                  mode: 'direct' | 'grounded' | 'no_context';
                  usedToolsWithoutKnowledgeBase: boolean;
                };
                answerMode = modeData.mode;
                usedToolsWithoutKnowledgeBase = modeData.usedToolsWithoutKnowledgeBase;
                if (hasProgressiveMsg) {
                  replaceLastMessage({
                    type: 'assistant',
                    content: fullResponse,
                    answerMode,
                    usedToolsWithoutKnowledgeBase,
                  });
                }
              } else if (msg.type === 'error') {
                onError((msg.data as string) || '流式请求服务端错误');
              }
            } catch {
              /* skip unparseable messages */
            }
          },
          onerror(err) {
            throw err;
          },
        });

        if (fullResponse) {
          if (hasProgressiveMsg) {
            replaceLastMessage({
              type: 'assistant',
              content: fullResponse,
              answerMode,
              usedToolsWithoutKnowledgeBase,
            });
          } else {
            addMessage({
              type: 'assistant',
              content: fullResponse,
              answerMode,
              usedToolsWithoutKnowledgeBase,
            });
          }
        }
      } catch (err: unknown) {
        const wasAborted = controller.signal.aborted || (err instanceof Error && err.name === 'AbortError');
        if (!wasAborted) {
          const errorMessage =
            err instanceof Error ? err.message : '流式请求失败';
          onError(errorMessage);
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [sessionId, addMessage, replaceLastMessage, setStreaming],
  );

  return { sendStream, abort };
}
