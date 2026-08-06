import { Route, Routes, useLocation } from 'react-router';
import { LoginPage } from '../pages/login/LoginPage';
import { AppShell } from '../shell/AppShell';
import { HomePage } from '../shell/HomePage';
import { NotFoundPage } from '../shell/NotFoundPage';
import { parseDrawerLocation } from './drawer-params';
import { RedirectIfAuthenticated, RequireAuth } from './guards';

/*
 * 路由：/login 为全角色共用唯一入口（已认证访问按角色重定向落地页）；
 * 业务页一律挂在 RequireAuth 之下（未认证 → /login）。
 * 抽屉开关与无限下钻层级以 URL 表达（./drawer-params）：抽屉打开路径（/settings、/admin
 * 路径段）与 / 渲染同一 HomePage 实例——抽屉是覆盖层，主页在其下保持挂载不卸载，
 * 滚动位置与输入草稿原样保留（fe-shared-shell 规格 §1；共用基座 §5.1）。
 */
function HomeOrNotFound() {
  const { pathname } = useLocation();
  if (pathname === '/' || parseDrawerLocation(pathname).open) {
    return <HomePage />;
  }
  return <NotFoundPage />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<RedirectIfAuthenticated />}>
        <Route path="/login" element={<LoginPage />} />
      </Route>
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route path="*" element={<HomeOrNotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
