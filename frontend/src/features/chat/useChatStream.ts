import { useRef, useCallback } from 'react';
import { fetchEventSource } from '@microsoft/fetch-event-source';
import { useChat } from './ChatContext';

interface StreamMessage {
  type: 'content' | 'done' | 'error';
  data?: string;
}

export function useChatStream() {
  const { sessionId, addMessage, replaceLastMessage, setStreaming } = useChat();
  const abortRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }, []);

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
                fullResponse += msg.data || '';
                if (!hasProgressiveMsg) {
                  addMessage({ type: 'assistant', content: fullResponse });
                  hasProgressiveMsg = true;
                } else {
                  replaceLastMessage({ type: 'assistant', content: fullResponse });
                }
              } else if (msg.type === 'done') {
                return;
              } else if (msg.type === 'error') {
                onError(msg.data || '流式请求服务端错误');
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
            replaceLastMessage({ type: 'assistant', content: fullResponse });
          } else {
            addMessage({ type: 'assistant', content: fullResponse });
          }
        }
      } catch (err: unknown) {
        const errorMessage =
          err instanceof Error ? err.message : '流式请求失败';
        onError(errorMessage);
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [sessionId, addMessage, replaceLastMessage, setStreaming],
  );

  return { sendStream, abort };
}
