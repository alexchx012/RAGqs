/*
 * 登录页集成测试（规格 §5）：真实 client + MSW 契约 mock 全链路。
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { Route, Routes, useLocation } from 'react-router';
import { describe, expect, it } from 'vitest';
import { createApiClient } from '../../api/client';
import { createAuthApi } from '../../auth/api';
import { createMemoryAuthHub } from '../../auth/channel';
import { AuthSessionStore } from '../../auth/session';
import { copy } from '../../copy';
import { mockAuth, mockServer } from '../../mocks/testing';
import { AppRoutes } from '../../router/AppRoutes';
import { AUTO_OPEN_ADMIN_DRAWER_STATE_KEY } from '../../router/landing';
import { renderWithAuth, renderWithShell } from '../../test/auth-fixtures';
import { LoginPage } from './LoginPage';

function createIntegrationStore(): AuthSessionStore {
  let store: AuthSessionStore;
  const client = createApiClient({
    getAccessToken: () => store.getState().token,
    refresh: () => store.refresh(),
  });
  store = new AuthSessionStore({
    api: createAuthApi(client),
    bus: createMemoryAuthHub().createBus(),
  });
  return store;
}

async function renderLoginPage(store: AuthSessionStore = createIntegrationStore()) {
  renderWithShell(<AppRoutes />, store, ['/login']);
  // bootstrap 静默 refresh（无 Cookie → 失败）后渲染登录页
  const heading = await screen.findByRole('heading', { name: copy.login.title });
  return { store, heading };
}

async function fillCredentials(
  user: ReturnType<typeof userEvent.setup>,
  username: string,
  password: string,
) {
  await user.type(screen.getByLabelText(copy.login.usernameLabel), username);
  await user.type(screen.getByLabelText(copy.login.passwordLabel), password);
}

describe('登录页（规格 §5）', () => {
  it('渲染纯登录表单：无注册/忘记密码/SSO/租户选择器，初始禁用登录键', async () => {
    const user = userEvent.setup();
    await renderLoginPage();
    const submit = screen.getByRole('button', { name: copy.login.submit });
    expect(submit).toBeDisabled();
    expect(screen.getByText(copy.login.guide)).toBeInTheDocument();

    await user.type(screen.getByLabelText(copy.login.usernameLabel), 'zhangsan');
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText(copy.login.passwordLabel), 'x');
    expect(submit).toBeEnabled();
  });

  it('Tab 序 = 用户名 → 密码 → 眼睛 → 登录键；Enter 提交', async () => {
    const user = userEvent.setup();
    const { store } = await renderLoginPage();
    // 填满凭据使登录键可聚焦（禁用态不参与 Tab 序）；blur 后从页面起点断言 Tab 序
    await fillCredentials(user, 'zhangsan', 'password123');
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(screen.getByLabelText(copy.login.usernameLabel)).toHaveFocus();
    await user.tab();
    expect(screen.getByLabelText(copy.login.passwordLabel)).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: copy.login.showPassword })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: copy.login.submit })).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(await screen.findByLabelText(copy.chat.composer.inputPlaceholder)).toBeInTheDocument();
    expect(store.getState().status).toBe('authenticated');
  });

  it('眼睛图标切换明文/掩码', async () => {
    const user = userEvent.setup();
    await renderLoginPage();
    const input = screen.getByLabelText(copy.login.passwordLabel);
    expect(input).toHaveAttribute('type', 'password');
    const toggle = screen.getByRole('button', { name: copy.login.showPassword });
    await user.click(toggle);
    expect(input).toHaveAttribute('type', 'text');
    expect(screen.getByRole('button', { name: copy.login.hidePassword })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await user.click(screen.getByRole('button', { name: copy.login.hidePassword }));
    expect(input).toHaveAttribute('type', 'password');
  });

  it('401 invalid_credentials：错误行 + 双框红态，任一框再输入即时清除', async () => {
    const user = userEvent.setup();
    await renderLoginPage();
    await fillCredentials(user, 'zhangsan', 'wrong');
    await user.click(screen.getByRole('button', { name: copy.login.submit }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.login.errorInvalidCredentials);
    expect(screen.getByLabelText(copy.login.usernameLabel)).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText(copy.login.passwordLabel)).toHaveAttribute('aria-invalid', 'true');

    await user.type(screen.getByLabelText(copy.login.passwordLabel), 'x');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByLabelText(copy.login.passwordLabel)).toHaveAttribute('aria-invalid', 'false');
  });

  it('429 too_many_attempts：按 retry_after_seconds 倒计时禁用登录键，期满恢复', async () => {
    mockAuth.config.rateLimitSeconds = 2;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        mockAuth.login('zhangsan', 'wrong', 'test');
      } catch {
        // 预期连续失败以触发限流
      }
    }
    const user = userEvent.setup();
    await renderLoginPage();
    await fillCredentials(user, 'zhangsan', 'password123');
    await user.click(screen.getByRole('button', { name: copy.login.submit }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.login.errorTooManyAttempts);
    const countdown = screen.getByRole('button', { name: copy.login.retryCountdown(2) });
    expect(countdown).toBeDisabled();

    expect(
      await screen.findByRole('button', { name: copy.login.submit }, { timeout: 4000 }),
    ).toBeEnabled();
  }, 10_000);

  it('5xx：服务不可用错误行，登录键恢复可点', async () => {
    mockServer.use(
      http.post('/v1/auth/login', () =>
        HttpResponse.json(
          { error: { code: 'internal_error', message: '', details: {}, request_id: 'req_mock_x' } },
          { status: 500 },
        ),
      ),
    );
    const user = userEvent.setup();
    await renderLoginPage();
    await fillCredentials(user, 'zhangsan', 'password123');
    await user.click(screen.getByRole('button', { name: copy.login.submit }));

    expect(await screen.findByRole('alert')).toHaveTextContent(copy.login.errorServiceUnavailable);
    expect(screen.getByRole('button', { name: copy.login.submit })).toBeEnabled();
  });

  it('登录成功：整页淡出后按角色落地（普通用户 → 聊天主页）', async () => {
    const user = userEvent.setup();
    const { store } = await renderLoginPage();
    await fillCredentials(user, 'zhangsan', 'password123');
    await user.click(screen.getByRole('button', { name: copy.login.submit }));

    expect(await screen.findByLabelText(copy.chat.composer.inputPlaceholder)).toBeInTheDocument();
    expect(store.getState()).toMatchObject({ status: 'authenticated', user: expect.objectContaining({ role: 'user' }) });
  });

  it('运维登录落地：聊天主页并携带「自动展开管理抽屉」导航 state', async () => {
    function StateProbe() {
      const location = useLocation();
      return <output data-testid="landing-state">{JSON.stringify(location.state)}</output>;
    }
    const store = createIntegrationStore();
    renderWithAuth(
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/" element={<StateProbe />} />
      </Routes>,
      store,
      ['/login'],
    );
    const user = userEvent.setup();
    await fillCredentials(user, 'ops-wang', 'password123');
    await user.click(screen.getByRole('button', { name: copy.login.submit }));

    const probe = await screen.findByTestId('landing-state');
    await waitFor(() => {
      expect(probe.textContent).toContain(`"${AUTO_OPEN_ADMIN_DRAWER_STATE_KEY}":true`);
    });
  });
});
