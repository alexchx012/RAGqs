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

const alice = {
  id: 'u1',
  username: 'alice',
  roles: ['viewer'],
  spaces: ['default'],
  version: 1,
  created_at: '2026-01-01T00:00:00Z',
};

function mockUsersList(users = [alice]) {
  return {
    code: 200,
    data: { users },
  };
}

describe('UserManagementPanel', () => {
  beforeEach(() => {
    mockApiJson.mockReset();
    mockApiJson.mockResolvedValue(mockUsersList());
  });
  afterEach(() => cleanup());

  it('lists users from GET /admin/users', async () => {
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    expect(mockApiJson).toHaveBeenCalledWith('/admin/users');
  });

  it('shows version conflict message on 409 update via edit→save flow', async () => {
    mockApiJson
      .mockResolvedValueOnce(mockUsersList())
      .mockRejectedValueOnce(new ApiError('administrator user version conflict', 409));

    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());

    // list mode must not expose bare save; enter edit mode first
    expect(screen.queryByTestId('save-user-u1')).toBeNull();
    fireEvent.click(screen.getByTestId('edit-user-u1'));
    await waitFor(() => expect(screen.getByTestId('save-user-u1')).toBeDefined());

    fireEvent.click(screen.getByTestId('save-user-u1'));
    await waitFor(() =>
      expect(screen.getByText('该用户已被其他操作修改，请刷新后重试')).toBeDefined(),
    );
  });

  it('DELETE request uses method DELETE and body with expected_version', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockApiJson
      .mockResolvedValueOnce(mockUsersList())
      .mockResolvedValueOnce({ code: 200, data: {} })
      .mockResolvedValueOnce(mockUsersList([]));

    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());

    fireEvent.click(screen.getByTestId('delete-user-u1'));

    await waitFor(() => {
      expect(mockApiJson).toHaveBeenCalledWith(
        '/admin/users/u1',
        expect.objectContaining({
          method: 'DELETE',
          body: JSON.stringify({ expected_version: 1 }),
        }),
      );
    });

    confirmSpy.mockRestore();
  });

  it('maps last-administrator error to 无法删除唯一的管理员账号', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockApiJson
      .mockResolvedValueOnce(mockUsersList())
      .mockRejectedValueOnce(new ApiError('cannot remove last administrator', 400));

    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());

    fireEvent.click(screen.getByTestId('delete-user-u1'));

    await waitFor(() =>
      expect(screen.getByText('无法删除唯一的管理员账号')).toBeDefined(),
    );

    confirmSpy.mockRestore();
  });
});
