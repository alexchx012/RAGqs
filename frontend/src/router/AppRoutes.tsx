import { Route, Routes } from 'react-router';
import { AppShell } from '../shell/AppShell';
import { NotFoundPage } from '../shell/NotFoundPage';
import { PlaceholderPage } from '../shell/PlaceholderPage';

/*
 * 路由基础设施：业务路由由后续 change 挂载。
 * 抽屉开关与无限下钻层级以 URL 表达的能力见 ./drawer-params（fe-shared-shell 在此之上实现状态机）。
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<PlaceholderPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
