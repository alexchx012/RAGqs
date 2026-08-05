import { Route, Routes } from 'react-router';
import { LoginPage } from '../pages/login/LoginPage';
import { AppShell } from '../shell/AppShell';
import { NotFoundPage } from '../shell/NotFoundPage';
import { PlaceholderPage } from '../shell/PlaceholderPage';
import { RedirectIfAuthenticated, RequireAuth } from './guards';

/*
 * 路由：/login 为全角色共用唯一入口（已认证访问按角色重定向落地页）；
 * 业务页一律挂在 RequireAuth 之下（未认证 → /login）。
 * 抽屉开关与无限下钻层级以 URL 表达的能力见 ./drawer-params（fe-shared-shell 在此之上实现状态机）。
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route element={<RedirectIfAuthenticated />}>
        <Route path="/login" element={<LoginPage />} />
      </Route>
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route index element={<PlaceholderPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
