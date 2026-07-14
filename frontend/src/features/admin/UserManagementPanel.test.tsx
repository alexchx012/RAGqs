import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import React from 'react';
import { apiJson, ApiError } from '../../api/client';
import UserManagementPanel from './UserManagementPanel';

vi.mock('../../api/client', () => ({
  apiJson: vi.fn(),
  ApiError: class extends Error {
    status?: number;
    constructor(message: string, status?: number) {
      super(message);
      this.status = status;
      this.name = 'ApiError';
    }
  },
}));

const mockApiJson = apiJson as unknown as ReturnType<typeof vi.fn>;

describe('UserManagementPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApiJson.mockResolvedValue({
      code: 200,
      data: {
        users: [
          {
            id: 'u1',
            username: 'alice',
            roles: ['viewer'],
            spaces: ['default'],
            version: 1,
            created_at: '2026-01-01T00:00:00Z',
          },
        ],
      },
    });
  });
  afterEach(() => cleanup());

  it('lists users from GET /admin/users', async () => {
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    expect(mockApiJson).toHaveBeenCalledWith('/admin/users');
  });

  it('shows version conflict message on 409 update', async () => {
    mockApiJson
      .mockResolvedValueOnce({
        code: 200,
        data: {
          users: [
            {
              id: 'u1',
              username: 'alice',
              roles: ['viewer'],
              spaces: ['default'],
              version: 1,
              created_at: '2026-01-01T00:00:00Z',
            },
          ],
        },
      })
      .mockRejectedValueOnce(new ApiError('administrator user version conflict', 409));

    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    fireEvent.click(screen.getByTestId('save-user-u1'));
    await waitFor(() =>
      expect(screen.getByText('该用户已被其他操作修改，请刷新后重试')).toBeDefined(),
    );
  });
});
