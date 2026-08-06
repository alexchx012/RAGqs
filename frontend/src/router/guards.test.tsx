/*
 * 路由守卫与按角色落地（规格 §4）。
 */

import { screen } from '@testing-library/react';
import { Route, Routes, useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { copy } from '../copy';
import { AppRoutes } from './AppRoutes';
import { RedirectIfAuthenticated, RequireAuth } from './guards';
import { AUTO_OPEN_ADMIN_DRAWER_STATE_KEY } from './landing';
import {
  createAuthedStore,
  createTestStore,
  fakeAuthApi,
  renderWithAuth,
  renderWithShell,
  testUser,
} from '../test/auth-fixtures';

function LandingProbe() {
  const location = useLocation();
  return <output data-testid="landing-state">{JSON.stringify(location.state)}</output>;
}

describe('路由守卫（规格 §4）', () => {
  it('未认证访问业务页 → 重定向 /login', async () => {
    const store = createTestStore(
      fakeAuthApi({
        refresh: vi.fn(async () =>
          Promise.reject(
            new ApiError({ status: 401, code: 'invalid_refresh', message: '', details: {}, requestId: null }),
          ),
        ),
      }),
    );
    renderWithAuth(<AppRoutes />, store, ['/']);
    expect(await screen.findByRole('heading', { name: copy.login.title })).toBeInTheDocument();
  });

  it('认证状态未知（静默 refresh 进行中）→ 渲染等待态，不渲染业务内容', () => {
    const store = createTestStore(
      fakeAuthApi({ refresh: vi.fn(() => new Promise<{ token: string }>(() => {})) }),
    );
    renderWithAuth(<AppRoutes />, store, ['/']);
    expect(screen.getByRole('status', { name: copy.shell.loading })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: copy.shell.placeholderTitle })).not.toBeInTheDocument();
  });

  it('已认证访问 /login → 按角色重定向落地页（普通用户 → 聊天主页）', async () => {
    const store = await createAuthedStore();
    renderWithShell(<AppRoutes />, store, ['/login']);
    expect(
      await screen.findByRole('heading', { name: copy.shell.placeholderTitle }),
    ).toBeInTheDocument();
  });

  it('已认证运维访问 /login → 聊天主页并携带「自动展开管理抽屉」导航 state', async () => {
    const store = await createAuthedStore(testUser({ role: 'ops', username: 'ops-wang' }));
    renderWithAuth(
      <Routes>
        <Route element={<RedirectIfAuthenticated />}>
          <Route path="/login" element={<output data-testid="login-route">login</output>} />
        </Route>
        <Route element={<RequireAuth />}>
          <Route path="/" element={<LandingProbe />} />
        </Route>
      </Routes>,
      store,
      ['/login'],
    );
    const probe = await screen.findByTestId('landing-state');
    expect(probe.textContent).toContain(`"${AUTO_OPEN_ADMIN_DRAWER_STATE_KEY}":true`);
  });
});
