import { useMemo } from 'react';
import { Route, Routes, useLocation } from 'react-router';
import { createApiClient } from '../api/client';
import { useAuthStore } from '../auth/AuthProvider';
import { LoginPage } from '../pages/login/LoginPage';
import { createPreviewApi } from '../preview/api';
import { PreviewPage } from '../preview/PreviewPage';
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
 * 原文预览页（fe-doc-preview）：/preview/:document_id 与 AppShell 平级——独立窗口页，
 * 不带侧边栏与抽屉（避免 DrawerHost/铃铛/通知轮询）。
 */
function HomeOrNotFound() {
  const { pathname } = useLocation();
  if (pathname === '/' || parseDrawerLocation(pathname).open) {
    return <HomePage />;
  }
  return <NotFoundPage />;
}

/** 预览页 API 装配：复用当前会话 store 的 token / refresh 链路（与 createAuth 同一接线）。 */
function PreviewRoute() {
  const store = useAuthStore();
  const api = useMemo(
    () =>
      createPreviewApi(
        createApiClient({
          getAccessToken: () => store.getState().token,
          getAuthSessionId: () => store.getAuthSessionId(),
          refresh: () => store.refresh(),
        }),
      ),
    [store],
  );
  return <PreviewPage api={api} />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<RedirectIfAuthenticated />}>
        <Route path="/login" element={<LoginPage />} />
      </Route>
      <Route element={<RequireAuth />}>
        <Route path="/preview/:document_id" element={<PreviewRoute />} />
        <Route element={<AppShell />}>
          <Route path="*" element={<HomeOrNotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
