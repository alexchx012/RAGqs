import { useCallback } from 'react';
import { useChat } from './ChatContext';
import { apiJson } from '../../api/client';
import type { ChatData } from '../../api/types';

export function useChatQuick() {
  const { sessionId, addMessage, setStreaming } = useChat();

  const sendQuick = useCallback(
    async (message: string, spaceId: string): Promise<void> => {
      setStreaming(true);
      try {
        const data = await apiJson<ChatData>('/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            Id: sessionId,
            Question: message,
            spaceId,
          }),
        });

        if (data.code === 200 && data.data?.success) {
          addMessage({
            type: 'assistant',
            content: data.data.answer || '（无回复）',
          });
        } else {
          throw new Error(
            data.data?.errorMessage || data.message || '请求失败',
          );
        }
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : '请求失败';
        addMessage({
          type: 'assistant',
          content: `错误: ${message}`,
        });
      } finally {
        setStreaming(false);
      }
    },
    [sessionId, addMessage, setStreaming],
  );

  return { sendQuick };
}
