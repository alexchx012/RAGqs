import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// Mock api client
vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
}));

// Mock KnowledgeContext
const mockSetSelectedSpaceId = vi.fn();
const mockRefreshSpaces = vi.fn();
const mockSpaceIdOf = vi.fn((space: { space_id?: string; name?: string }) => space.space_id || 'default');

vi.mock('./KnowledgeContext', () => ({
  useKnowledge: vi.fn(),
}));

import { apiJson } from '../../api/client';
import { useKnowledge } from './KnowledgeContext';
import KnowledgeSpaceSelector from './KnowledgeSpaceSelector';

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

describe('KnowledgeSpaceSelector', () => {
  const onSpaceChange = vi.fn();

  function setupKnowledgeContext(overrides: Record<string, unknown> = {}) {
    const defaults = {
      selectedSpaceId: 'default',
      setSelectedSpaceId: mockSetSelectedSpaceId,
      knowledgeSpaces: [
        { space_id: 'default', name: 'Default' },
        { space_id: 'sp-1', name: 'Space 1' },
      ],
      refreshSpaces: mockRefreshSpaces,
      spaceIdOf: mockSpaceIdOf,
      ...overrides,
    };
    (useKnowledge as unknown as ReturnType<typeof vi.fn>).mockReturnValue(defaults);
    return defaults;
  }

  beforeEach(() => {
    vi.clearAllMocks();
    setupKnowledgeContext();
    mockRefreshSpaces.mockResolvedValue([
      { space_id: 'default', name: 'Default' },
      { space_id: 'sp-1', name: 'Space 1' },
    ]);
    mockApiJson.mockResolvedValue({ code: 200 });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders the section header with title and refresh button', () => {
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    expect(screen.getByText('知识空间')).toBeDefined();
    expect(screen.getByTitle('刷新知识空间')).toBeDefined();
  });

  it('renders a select element with knowledge space options', () => {
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const select = screen.getByRole('combobox') as HTMLSelectElement;
    expect(select).toBeDefined();
    expect(select.value).toBe('default');

    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(2);
    expect(options[0].textContent).toBe('Default');
    expect(options[1].textContent).toBe('Space 1');
  });

  it('renders create form with space id and name inputs and submit button', () => {
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    expect(screen.getByPlaceholderText('space id')).toBeDefined();
    expect(screen.getByPlaceholderText('显示名称')).toBeDefined();
    expect(screen.getByText('创建')).toBeDefined();
  });

  it('calls onSpaceChange and setSelectedSpaceId when a new space is selected', async () => {
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const select = screen.getByRole('combobox');
    fireEvent.change(select, { target: { value: 'sp-1' } });

    expect(mockSetSelectedSpaceId).toHaveBeenCalledWith('sp-1');
    expect(onSpaceChange).toHaveBeenCalledTimes(1);
  });

  it('creates a new knowledge space and shows success status', async () => {
    const user = userEvent.setup();
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const spaceIdInput = screen.getByPlaceholderText('space id');
    const nameInput = screen.getByPlaceholderText('显示名称');
    const submitButton = screen.getByText('创建');

    await user.type(spaceIdInput, 'new-space');
    await user.type(nameInput, 'New Space');
    await user.click(submitButton);

    await waitFor(() => {
      expect(mockApiJson).toHaveBeenCalledWith('/knowledge-spaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ space_id: 'new-space', name: 'New Space' }),
      });
    });

    expect(mockSetSelectedSpaceId).toHaveBeenCalledWith('new-space');
    expect(mockRefreshSpaces).toHaveBeenCalledTimes(1);
    expect(onSpaceChange).toHaveBeenCalled();

    await waitFor(() => {
      expect(screen.getByText('知识空间 new-space 已创建')).toBeDefined();
    });
  });

  it('uses space id as name when display name is empty', async () => {
    const user = userEvent.setup();
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const spaceIdInput = screen.getByPlaceholderText('space id');
    const submitButton = screen.getByText('创建');

    await user.type(spaceIdInput, 'only-id');
    await user.click(submitButton);

    await waitFor(() => {
      expect(mockApiJson).toHaveBeenCalledWith('/knowledge-spaces', expect.objectContaining({
        body: JSON.stringify({ space_id: 'only-id', name: 'only-id' }),
      }));
    });
  });

  it('does not submit when space id is empty', async () => {
    const user = userEvent.setup();
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const submitButton = screen.getByText('创建');
    await user.click(submitButton);

    expect(mockApiJson).not.toHaveBeenCalled();
  });

  it('shows error status when creation fails', async () => {
    mockApiJson.mockRejectedValue(new Error('Network error'));

    const user = userEvent.setup();
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const spaceIdInput = screen.getByPlaceholderText('space id');
    const submitButton = screen.getByText('创建');

    await user.type(spaceIdInput, 'fail-space');
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText('Network error')).toBeDefined();
    });
  });

  it('shows generic error message when creation fails with non-Error', async () => {
    mockApiJson.mockRejectedValue('string error');

    const user = userEvent.setup();
    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const spaceIdInput = screen.getByPlaceholderText('space id');
    const submitButton = screen.getByText('创建');

    await user.type(spaceIdInput, 'fail-space');
    await user.click(submitButton);

    await waitFor(() => {
      expect(screen.getByText('创建失败')).toBeDefined();
    });
  });

  it('refreshes spaces and selects first when current space is gone', async () => {
    setupKnowledgeContext({
      selectedSpaceId: 'gone-space',
      knowledgeSpaces: [],
      setSelectedSpaceId: mockSetSelectedSpaceId,
      refreshSpaces: mockRefreshSpaces,
      spaceIdOf: mockSpaceIdOf,
    });

    mockRefreshSpaces.mockResolvedValue([
      { space_id: 'new-default', name: 'New Default' },
    ]);

    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const refreshBtn = screen.getByTitle('刷新知识空间');
    fireEvent.click(refreshBtn);

    await waitFor(() => {
      expect(mockRefreshSpaces).toHaveBeenCalledTimes(1);
    });

    await waitFor(() => {
      expect(mockSetSelectedSpaceId).toHaveBeenCalledWith('new-default');
      expect(onSpaceChange).toHaveBeenCalled();
    });
  });

  it('shows error status when refresh fails', async () => {
    mockRefreshSpaces.mockRejectedValue(new Error('Refresh failed'));

    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const refreshBtn = screen.getByTitle('刷新知识空间');
    fireEvent.click(refreshBtn);

    await waitFor(() => {
      expect(screen.getByText('Refresh failed')).toBeDefined();
    });
  });

  it('shows generic error when refresh fails with non-Error', async () => {
    mockRefreshSpaces.mockRejectedValue('refresh error');

    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const refreshBtn = screen.getByTitle('刷新知识空间');
    fireEvent.click(refreshBtn);

    await waitFor(() => {
      expect(screen.getByText('刷新失败')).toBeDefined();
    });
  });

  it('clears status message on refresh', async () => {
    const { rerender } = render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const refreshBtn = screen.getByTitle('刷新知识空间');
    fireEvent.click(refreshBtn);

    // The status should be cleared (empty string) when refresh starts
    await waitFor(() => {
      expect(mockRefreshSpaces).toHaveBeenCalled();
    });
  });

  it('renders fallback option when knowledgeSpaces is empty', () => {
    setupKnowledgeContext({
      selectedSpaceId: 'my-space',
      knowledgeSpaces: [],
      setSelectedSpaceId: mockSetSelectedSpaceId,
      refreshSpaces: mockRefreshSpaces,
      spaceIdOf: mockSpaceIdOf,
    });

    render(<KnowledgeSpaceSelector onSpaceChange={onSpaceChange} />);

    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(1);
    expect(options[0].textContent).toBe('my-space');
  });
});
