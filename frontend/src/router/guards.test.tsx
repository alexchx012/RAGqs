/*
 * 路由守卫与按角色落地（规格 §4；深链回跳 P1#12）。
 */

import { render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/errors';
import { AuthProvider } from '../auth/AuthProvider';
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

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-path">{location.pathname}</output>;
}

function renderWithRoutes(ui: ReactElement, store: Parameters<typeof AuthProvider>[0]['store'], initialEntry: Parameters<typeof MemoryRouter>[0]['initialEntries']) {
  return render(
    <AuthProvider store={store}>
      <MemoryRouter initialEntries={initialEntry}>{ui}</MemoryRouter>
    </AuthProvider>,
  );
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
    expect(
      screen.queryByLabelText(copy.chat.composer.inputPlaceholder),
    ).not.toBeInTheDocument();
  });

  it('已认证访问 /login → 按角色重定向落地页（普通用户 → 聊天主页）', async () => {
    const store = await createAuthedStore();
    renderWithShell(<AppRoutes />, store, ['/login']);
    expect(
      await screen.findByLabelText(copy.chat.composer.inputPlaceholder),
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

  it('未认证访问受保护深链，登录成功后经 parseDrawerLocation 校验回跳原目标', async () => {
    // refresh 拒绝（401）：store 保持未认证 → 深链重定向 /login（携 from）；
    // store.login 成功后由 RedirectIfAuthenticated 消费 from 回跳（轻量路由，聚焦守卫行为）
    const store = createTestStore(
      fakeAuthApi({
        refresh: vi.fn(async () =>
          Promise.reject(
            new ApiError({ status: 401, code: 'invalid_refresh', message: '', details: {}, requestId: null }),
          ),
        ),
      }),
    );
    renderWithAuth(
      <>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/settings/*" element={<output data-testid="deep-target">deep</output>} />
          </Route>
          <Route element={<RedirectIfAuthenticated />}>
            <Route path="/login" element={<p>login</p>} />
          </Route>
        </Routes>
        <LocationProbe />
      </>,
      store,
      ['/settings/knowledge/uploads'],
    );
    await waitFor(() => expect(screen.getByText('login')).toBeInTheDocument());
    await store.login('zhangsan', 'correct-horse');
    // from 为合法抽屉路径 → 回原目标（而非角色落地页）
    expect(await screen.findByTestId('deep-target')).toBeInTheDocument();
    expect(screen.getByTestId('location-path')).toHaveTextContent('/settings/knowledge/uploads');
  });

  it('from 为非抽屉路径（非法）时回角色落地页，不回跳', async () => {
    const store = await createAuthedStore();
    renderWithRoutes(
      <Routes>
        <Route element={<RedirectIfAuthenticated />}>
          <Route path="/login" element={<p>login</p>} />
        </Route>
        <Route path="*" element={<LandingProbe />} />
      </Routes>,
      store,
      [{ pathname: '/login', state: { from: '/preview/doc_1' } }],
    );
    expect(await screen.findByTestId('landing-state')).toBeInTheDocument();
    expect(screen.getByTestId('landing-state').textContent).toBe('null'); // landingTargetFor 默认无 state
  });
});
