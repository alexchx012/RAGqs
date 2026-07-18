import React, { useCallback, useEffect, useRef, useState } from 'react';
import { apiJson, ApiError } from '../../api/client';
import type { AdminUser, AdminUsersData, PanelState } from '../../api/types';

const ALL_ROLES = ['admin', 'viewer', 'uploader', 'maintainer', 'auditor', 'ops'] as const;

function mapAdminUserError(err: unknown): string {
  if (err instanceof ApiError) {
    if (
      err.message.includes('version conflict') ||
      err.message.includes('administrator user version conflict')
    ) {
      return '该用户已被其他操作修改，请刷新后重试';
    }
    if (
      err.message.includes('last administrator') ||
      err.message.includes('cannot remove last administrator')
    ) {
      return '无法删除唯一的管理员账号';
    }
    if (err.message.includes('already exists')) {
      return '用户名已存在';
    }
    if (err.status === 403) {
      return '无权限执行此操作（需要 user:manage 权限）';
    }
    return err.message;
  }
  return '操作失败，请重试';
}

function parseSpaces(input: string): string[] {
  return input
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

export default function UserManagementPanel() {
  const [panelState, setPanelState] = useState<PanelState<AdminUser>>({ status: 'loading' });
  const [actionError, setActionError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editRoles, setEditRoles] = useState<string[]>([]);
  const [editSpaces, setEditSpaces] = useState('');
  const [createUsername, setCreateUsername] = useState('');
  const [createPassword, setCreatePassword] = useState('');
  const [createRoles, setCreateRoles] = useState<string[]>(['viewer']);
  const [createSpaces, setCreateSpaces] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const latestRequestRef = useRef(0);

  const loadUsers = useCallback(async () => {
    const requestId = ++latestRequestRef.current;
    setPanelState({ status: 'loading' });
    setActionError(null);
    try {
      const data = await apiJson<AdminUsersData>('/admin/users');
      if (requestId !== latestRequestRef.current) return;
      const users = Array.isArray(data.data?.users) ? data.data.users : [];
      setPanelState(users.length === 0 ? { status: 'empty' } : { status: 'ready', items: users });
    } catch (err: unknown) {
      if (requestId !== latestRequestRef.current) return;
      setPanelState({ status: 'error', message: mapAdminUserError(err) });
    }
  }, []);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  function startEdit(user: AdminUser) {
    setEditingId(user.id);
    setEditRoles([...user.roles]);
    setEditSpaces(user.spaces.join(', '));
    setActionError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditRoles([]);
    setEditSpaces('');
  }

  function toggleRole(role: string, current: string[], setter: (next: string[]) => void) {
    if (current.includes(role)) {
      // keep at least one role selected
      if (current.length <= 1) return;
      setter(current.filter((r) => r !== role));
    } else {
      setter([...current, role]);
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (createRoles.length === 0) {
      setActionError('请至少选择一个角色');
      return;
    }
    setActionError(null);
    setSubmitting(true);
    try {
      await apiJson('/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: createUsername,
          password: createPassword,
          roles: createRoles,
          spaces: parseSpaces(createSpaces),
        }),
      });
      setCreateUsername('');
      setCreatePassword('');
      setCreateRoles(['viewer']);
      setCreateSpaces('');
      await loadUsers();
    } catch (err: unknown) {
      setActionError(mapAdminUserError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSave(user: AdminUser) {
    if (editRoles.length === 0) {
      setActionError('请至少选择一个角色');
      return;
    }
    setActionError(null);
    setSubmitting(true);
    try {
      await apiJson(`/admin/users/${encodeURIComponent(user.id)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_version: user.version,
          roles: editRoles,
          spaces: parseSpaces(editSpaces),
        }),
      });
      setEditingId(null);
      await loadUsers();
    } catch (err: unknown) {
      setActionError(mapAdminUserError(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(user: AdminUser) {
    if (!window.confirm(`确认删除用户「${user.username}」？`)) return;
    setActionError(null);
    setSubmitting(true);
    try {
      await apiJson(`/admin/users/${encodeURIComponent(user.id)}`, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_version: user.version }),
      });
      if (editingId === user.id) cancelEdit();
      await loadUsers();
    } catch (err: unknown) {
      setActionError(mapAdminUserError(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="user-management-panel" data-testid="user-management-panel">
      <div className="ops-section-header">
        <h2>用户管理</h2>
        <button type="button" className="ops-icon-btn" title="刷新用户" onClick={loadUsers}>
          ↻
        </button>
      </div>

      {actionError && (
        <div className="user-management-error" role="alert" data-testid="user-action-error">
          {actionError}
        </div>
      )}

      <div className="ops-list">
        {panelState.status === 'loading' && <div>加载中...</div>}
        {panelState.status === 'error' && (
          <div style={{ color: '#b3261e' }}>
            {panelState.message}{' '}
            <button type="button" onClick={loadUsers}>
              ↻ 重试
            </button>
          </div>
        )}
        {panelState.status === 'empty' && <div>暂无用户</div>}
        {panelState.status === 'ready' &&
          panelState.items.map((user) => (
            <div key={user.id} className="ops-row" data-testid={`user-row-${user.id}`}>
              {editingId === user.id ? (
                <div className="user-edit-form">
                  <div className="ops-row-main">{user.username}</div>
                  <div className="role-checkboxes">
                    {ALL_ROLES.map((role) => (
                      <label key={role}>
                        <input
                          type="checkbox"
                          checked={editRoles.includes(role)}
                          onChange={() => toggleRole(role, editRoles, setEditRoles)}
                        />
                        {role}
                      </label>
                    ))}
                  </div>
                  <label>
                    空间（逗号分隔）
                    <input
                      value={editSpaces}
                      onChange={(e) => setEditSpaces(e.target.value)}
                      data-testid={`edit-spaces-${user.id}`}
                    />
                  </label>
                  <div className="ops-row-actions">
                    <button
                      type="button"
                      data-testid={`save-user-${user.id}`}
                      disabled={submitting}
                      onClick={() => handleSave(user)}
                    >
                      保存
                    </button>
                    <button type="button" onClick={cancelEdit} disabled={submitting}>
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="ops-row-main">{user.username}</div>
                  <div className="ops-row-meta">
                    roles: {user.roles.join(', ') || '—'} · spaces:{' '}
                    {user.spaces.join(', ') || '—'} · v{user.version}
                  </div>
                  <div className="ops-row-actions">
                    <button
                      type="button"
                      data-testid={`edit-user-${user.id}`}
                      onClick={() => startEdit(user)}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      data-testid={`delete-user-${user.id}`}
                      disabled={submitting}
                      onClick={() => handleDelete(user)}
                    >
                      删除
                    </button>
                  </div>
                </>
              )}
            </div>
          ))}
      </div>

      <form className="user-create-form" onSubmit={handleCreate} data-testid="user-create-form">
        <h3>创建用户</h3>
        <label>
          用户名
          <input
            value={createUsername}
            onChange={(e) => setCreateUsername(e.target.value)}
            required
            data-testid="create-username"
          />
        </label>
        <label>
          密码
          <input
            type="password"
            value={createPassword}
            onChange={(e) => setCreatePassword(e.target.value)}
            required
            data-testid="create-password"
          />
        </label>
        <div className="role-checkboxes">
          {ALL_ROLES.map((role) => (
            <label key={role}>
              <input
                type="checkbox"
                checked={createRoles.includes(role)}
                onChange={() => toggleRole(role, createRoles, setCreateRoles)}
              />
              {role}
            </label>
          ))}
        </div>
        <label>
          空间（逗号分隔）
          <input
            value={createSpaces}
            onChange={(e) => setCreateSpaces(e.target.value)}
            data-testid="create-spaces"
          />
        </label>
        <button type="submit" disabled={submitting || !createUsername || !createPassword}>
          {submitting ? '提交中...' : '创建'}
        </button>
      </form>
    </section>
  );
}
