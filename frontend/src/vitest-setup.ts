import { vi } from 'vitest';

// Polyfill scrollIntoView for jsdom
Element.prototype.scrollIntoView = vi.fn();
