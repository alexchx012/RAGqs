/*
 * 优化输入生产接缝（prompt-enhance 规格 §3.2）：把 ChatApi.enhancePrompt 包装成 composer 的
 * onEnhance 形状。失败（非中止）先经 onFailed 让调用方弹错误提示，再 rethrow 交给 composer
 * 还原原文；用户中止（AbortError / 信号已中止）静默，只 rethrow 不提示。
 */

import type { ChatApi } from './api';

export function createPromptEnhanceHandler(
  api: Pick<ChatApi, 'enhancePrompt'>,
  onFailed: () => void,
): (prompt: string, signal?: AbortSignal) => Promise<string> {
  return async (prompt, signal) => {
    try {
      return await api.enhancePrompt(prompt, signal);
    } catch (error) {
      const aborted =
        signal?.aborted === true || (error instanceof DOMException && error.name === 'AbortError');
      if (!aborted) {
        onFailed();
      }
      throw error;
    }
  };
}
