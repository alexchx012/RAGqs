import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import FileUpload from './FileUpload';
import { useChat } from '../chat/ChatContext';

// Mock useChat
vi.mock('../chat/ChatContext', () => ({
  useChat: vi.fn(),
}));

const mockUseChat = useChat as unknown as ReturnType<typeof vi.fn>;

// Mock global fetch
const mockFetch = vi.fn();

describe('FileUpload', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseChat.mockReturnValue({
      addMessage: vi.fn(),
    });
    globalThis.fetch = mockFetch;
  });

  afterEach(() => {
    cleanup();
  });

  it('renders the upload button', () => {
    render(
      <FileUpload spaceId="space-1" onRefresh={vi.fn()} />,
    );

    const btn = screen.getByTitle('上传文件');
    expect(btn).toBeDefined();
    expect(btn.tagName).toBe('BUTTON');
  });

  it('renders upload button with disabled state', () => {
    render(
      <FileUpload spaceId="space-1" disabled={true} onRefresh={vi.fn()} />,
    );

    const btn = screen.getByTitle('上传文件') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it('renders hidden file input with accepted types', () => {
    render(
      <FileUpload spaceId="space-1" onRefresh={vi.fn()} />,
    );

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toBeDefined();
    expect(input.accept).toBe('.txt,.md,.markdown,.csv,.html,.htm,.json');
  });

  it('uploads file successfully and calls addMessage and onRefresh', async () => {
    const addMessage = vi.fn();
    const onRefresh = vi.fn();
    mockUseChat.mockReturnValue({ addMessage });

    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ code: 200 }),
    });

    render(
      <FileUpload spaceId="space-1" onRefresh={onRefresh} />,
    );

    const file = new File(['hello world'], 'test.txt', { type: 'text/plain' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, file);

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledWith(
        '/api/upload?space_id=space-1',
        expect.objectContaining({ method: 'POST' }),
      );
    });

    await waitFor(() => {
      expect(addMessage).toHaveBeenCalledWith({
        type: 'assistant',
        content: '✅ 文件 "test.txt" 上传成功，已建立向量索引。',
      });
    });

    await waitFor(() => {
      expect(onRefresh).toHaveBeenCalled();
    });
  });

  it('shows uploading state with different title during upload', async () => {
    const addMessage = vi.fn();
    mockUseChat.mockReturnValue({ addMessage });

    // Never resolves so we can test uploading state
    mockFetch.mockReturnValue(new Promise(() => {}));

    render(
      <FileUpload spaceId="space-1" onRefresh={vi.fn()} />,
    );

    const file = new File(['hello world'], 'test.txt', { type: 'text/plain' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, file);

    await waitFor(() => {
      const btn = screen.getByTitle('上传中...');
      expect(btn).toBeDefined();
    });
  });

  it('handles API error response gracefully', async () => {
    const addMessage = vi.fn();
    mockUseChat.mockReturnValue({ addMessage });

    mockFetch.mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ code: 400, detail: '文件过大' }),
    });

    render(
      <FileUpload spaceId="space-1" onRefresh={vi.fn()} />,
    );

    const file = new File(['hello world'], 'test.txt', { type: 'text/plain' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, file);

    await waitFor(() => {
      expect(addMessage).toHaveBeenCalledWith({
        type: 'assistant',
        content: '❌ 上传失败: 文件过大',
      });
    });
  });

  it('handles network error gracefully', async () => {
    const addMessage = vi.fn();
    mockUseChat.mockReturnValue({ addMessage });

    mockFetch.mockRejectedValueOnce(new Error('网络错误'));

    render(
      <FileUpload spaceId="space-1" onRefresh={vi.fn()} />,
    );

    const file = new File(['hello world'], 'test.txt', { type: 'text/plain' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    await userEvent.upload(input, file);

    await waitFor(() => {
      expect(addMessage).toHaveBeenCalledWith({
        type: 'assistant',
        content: '❌ 上传出错: 网络错误',
      });
    });
  });

  it('disables button when both disabled prop and uploading', () => {
    const addMessage = vi.fn();
    mockUseChat.mockReturnValue({ addMessage });

    render(
      <FileUpload spaceId="space-1" disabled={true} onRefresh={vi.fn()} />,
    );

    const btn = screen.getByTitle('上传文件') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});
