import '@testing-library/jest-dom/vitest';

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
