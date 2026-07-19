import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, act } from '@testing-library/react';
import React from 'react';
import { apiJson, ApiError } from '../../api/client';
import type { AdminUser } from '../../api/types';
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

const alice: AdminUser = {
  id: 'u1',
  username: 'alice',
  roles: ['viewer'],
  spaces: ['default'],
  department_id: null,
  version: 1,
  created_at: '2026-01-01T00:00:00Z',
};

const departments = [
  {
    id: 'dept-1',
    name: '工程部',
    description: null,
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'dept-2',
    name: '运维部',
    description: '负责运维',
    created_at: '2026-01-02T00:00:00Z',
  },
];

function mockUsersList(users = [alice]) {
  return {
    code: 200,
    data: { users },
  };
}

function defaultApiResponse(path: string, options?: RequestInit) {
  if (options) return Promise.resolve({ code: 200, data: {} });
  if (path === '/admin/users') return Promise.resolve(mockUsersList());
  if (path === '/admin/departments') {
    return Promise.resolve({ code: 200, data: { departments } });
  }
  return Promise.reject(new Error(`unexpected API path: ${path}`));
}

function rejectAction(method: string, error: Error) {
  mockApiJson.mockImplementation((path: string, options?: RequestInit) =>
    options?.method === method
      ? Promise.reject(error)
      : defaultApiResponse(path, options),
  );
}

describe('UserManagementPanel', () => {
  beforeEach(() => {
    mockApiJson.mockReset();
    mockApiJson.mockImplementation(defaultApiResponse);
  });
  afterEach(() => cleanup());

  it('lists users from GET /admin/users', async () => {
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    expect(mockApiJson).toHaveBeenCalledWith('/admin/users');
  });

  it('shows version conflict message on 409 update via edit→save flow', async () => {
    rejectAction('PATCH', new ApiError('administrator user version conflict', 409));

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

  it('ignores a stale refresh response when a newer refresh resolves first', async () => {
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());

    let resolveFirst: (value: unknown) => void;
    let resolveSecond: (value: unknown) => void;
    const firstPromise = new Promise((resolve) => { resolveFirst = resolve; });
    const secondPromise = new Promise((resolve) => { resolveSecond = resolve; });
    mockApiJson
      .mockImplementationOnce(() => firstPromise)
      .mockImplementationOnce(() => secondPromise);

    const refreshBtn = screen.getByTitle('刷新用户');
    fireEvent.click(refreshBtn); // stale, slower
    fireEvent.click(refreshBtn); // fresh, resolves first

    await act(async () => {
      resolveSecond!(mockUsersList([{ ...alice, id: 'u2', username: 'bob' }]));
      await secondPromise;
    });
    await waitFor(() => expect(screen.getByText('bob')).toBeDefined());

    // The stale first request resolves later — it must not overwrite bob with alice.
    await act(async () => {
      resolveFirst!(mockUsersList([alice]));
      await firstPromise;
    });
    expect(screen.getByText('bob')).toBeDefined();
    expect(screen.queryByText('alice')).toBeNull();
  });

  it('maps last-administrator error to 无法删除唯一的管理员账号', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    rejectAction('DELETE', new ApiError('cannot remove last administrator', 400));

    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());

    fireEvent.click(screen.getByTestId('delete-user-u1'));

    await waitFor(() =>
      expect(screen.getByText('无法删除唯一的管理员账号')).toBeDefined(),
    );

    confirmSpy.mockRestore();
  });

  it('loads department options and initializes the edit selection', async () => {
    mockApiJson.mockImplementation((path: string, options?: RequestInit) => {
      if (!options && path === '/admin/users') {
        return Promise.resolve(
          mockUsersList([{ ...alice, department_id: 'dept-1' }]),
        );
      }
      return defaultApiResponse(path, options);
    });
    render(<UserManagementPanel />);
    await waitFor(() => {
      expect(mockApiJson).toHaveBeenCalledWith('/admin/departments');
    });
    const createSelect = screen.getByTestId(
      'create-department',
    ) as HTMLSelectElement;
    expect(Array.from(createSelect.options).map((option) => option.text)).toEqual([
      '无部门',
      '工程部',
      '运维部',
    ]);
    fireEvent.click(screen.getByTestId('edit-user-u1'));
    expect(
      (screen.getByTestId('edit-department-u1') as HTMLSelectElement).value,
    ).toBe('dept-1');
  });

  it('includes the selected department when creating a user', async () => {
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    fireEvent.change(screen.getByTestId('create-username'), {
      target: { value: 'bob' },
    });
    fireEvent.change(screen.getByTestId('create-password'), {
      target: { value: 'secret' },
    });
    fireEvent.change(screen.getByTestId('create-department'), {
      target: { value: 'dept-1' },
    });
    fireEvent.submit(screen.getByTestId('user-create-form'));
    await waitFor(() => {
      expect(mockApiJson).toHaveBeenCalledWith(
        '/admin/users',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            username: 'bob',
            password: 'secret',
            roles: ['viewer'],
            spaces: [],
            department_id: 'dept-1',
          }),
        }),
      );
    });
  });

  it.each([
    {
      initialDepartment: 'dept-1',
      selectedDepartment: '',
      departmentPatch: { clear_department: true },
    },
    {
      initialDepartment: null,
      selectedDepartment: 'dept-2',
      departmentPatch: { department_id: 'dept-2' },
    },
  ])(
    'resends the selected department state on edit',
    async ({ initialDepartment, selectedDepartment, departmentPatch }) => {
      mockApiJson.mockImplementation((path: string, options?: RequestInit) => {
        if (!options && path === '/admin/users') {
          return Promise.resolve(
            mockUsersList([{ ...alice, department_id: initialDepartment }]),
          );
        }
        return defaultApiResponse(path, options);
      });
      render(<UserManagementPanel />);
      await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
      fireEvent.click(screen.getByTestId('edit-user-u1'));
      fireEvent.change(screen.getByTestId('edit-department-u1'), {
        target: { value: selectedDepartment },
      });
      fireEvent.click(screen.getByTestId('save-user-u1'));
      await waitFor(() => {
        expect(mockApiJson).toHaveBeenCalledWith(
          '/admin/users/u1',
          expect.objectContaining({
            method: 'PATCH',
            body: JSON.stringify({
              expected_version: 1,
              roles: ['viewer'],
              spaces: ['default'],
              ...departmentPatch,
            }),
          }),
        );
      });
    },
  );

  it('offers current administrator roles and omits retired admin', async () => {
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    expect(screen.getByLabelText('super_admin')).toBeDefined();
    expect(screen.getByLabelText('department_admin')).toBeDefined();
    expect(screen.queryByLabelText('admin')).toBeNull();
  });

  it('reports department request failures and retries gated actions', async () => {
    let departmentAttempts = 0;
    mockApiJson.mockImplementation((path: string, options?: RequestInit) => {
      if (!options && path === '/admin/departments') {
        departmentAttempts += 1;
        if (departmentAttempts === 1) {
          return Promise.reject(new ApiError('department directory unavailable', 503));
        }
      }
      return defaultApiResponse(path, options);
    });
    render(<UserManagementPanel />);
    const alert = await screen.findByTestId('department-load-error');
    expect(alert.getAttribute('role')).toBe('alert');
    expect(alert.textContent).toContain('department directory unavailable');
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    fireEvent.change(screen.getByTestId('create-username'), {
      target: { value: 'bob' },
    });
    fireEvent.change(screen.getByTestId('create-password'), {
      target: { value: 'secret' },
    });
    const createButton = screen.getByRole('button', { name: '创建' }) as HTMLButtonElement;
    expect(createButton.disabled).toBe(true);
    expect((screen.getByTestId('delete-user-u1') as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId('edit-user-u1'));
    expect((screen.getByTestId('save-user-u1') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId('retry-departments'));
    await waitFor(() => expect(createButton.disabled).toBe(false));
    expect(screen.queryByTestId('department-load-error')).toBeNull();
    expect(departmentAttempts).toBe(2);
    expect((screen.getByTestId('save-user-u1') as HTMLButtonElement).disabled).toBe(false);
  });

  it('treats a malformed successful directory payload as loaded empty', async () => {
    mockApiJson.mockImplementation((path: string, options?: RequestInit) => {
      if (!options && path === '/admin/departments') {
        return Promise.resolve({ code: 200, data: { departments: {} } });
      }
      return defaultApiResponse(path, options);
    });
    render(<UserManagementPanel />);
    await waitFor(() => expect(screen.getByText('alice')).toBeDefined());
    expect(screen.queryByTestId('department-load-error')).toBeNull();
    const select = screen.getByTestId('create-department') as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.text)).toEqual(['无部门']);
    fireEvent.change(screen.getByTestId('create-username'), {
      target: { value: 'bob' },
    });
    fireEvent.change(screen.getByTestId('create-password'), {
      target: { value: 'secret' },
    });
    expect((screen.getByRole('button', { name: '创建' }) as HTMLButtonElement).disabled)
      .toBe(false);
  });
});
