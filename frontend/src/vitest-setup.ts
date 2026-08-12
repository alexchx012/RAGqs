import '@testing-library/jest-dom/vitest';
import { afterAll, beforeAll, beforeEach } from 'vitest';
import {
  mockServer,
  resetMockAdmin,
  resetMockAuth,
  resetMockChat,
  resetMockKnowledge,
  resetMockNotifications,
  resetMockPreview,
} from './mocks/testing';

// 契约 mock（规格 §1）：全部用例在同一进程内经 MSW 访问模拟服务端，用例间状态复位
beforeAll(() => {
  mockServer.listen({ onUnhandledRequest: 'error' });
});

beforeEach(() => {
  mockServer.resetHandlers();
  resetMockAuth();
  resetMockNotifications();
  resetMockChat();
  resetMockKnowledge();
  resetMockAdmin();
  resetMockPreview();
});

afterAll(() => {
  mockServer.close();
});

// jsdom 未实现 matchMedia；theme 模块与其测试需要它。
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = ((query: string): MediaQueryList => {
    const list: MediaQueryList = {
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    };
    return list;
  }) as typeof window.matchMedia;
}
