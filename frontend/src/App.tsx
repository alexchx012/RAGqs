import { BrowserRouter } from 'react-router';
import { createAdminApi } from './admin/api';
import { AdminProvider } from './admin/AdminProvider';
import { AuthProvider } from './auth/AuthProvider';
import { createAuth } from './auth/create-auth';
import { EscStackProvider } from './lib/esc-stack-provider';
import { createNotificationsApi } from './notifications/api';
import { NotificationsProvider } from './notifications/NotificationsProvider';
import { NotificationsStore } from './notifications/store';
import { AppRoutes } from './router/AppRoutes';
import { createDrawerRegistry, DrawerRegistryProvider } from './shell/drawer/DrawerRegistryProvider';
import { createSettingsApi } from './settings/api';
import { SettingsProvider } from './settings/SettingsProvider';
import { initTheme } from './theme/theme';

// 认证层单例：随页面加载创建；页面刷新后内存 token 丢失，由 bootstrap 静默 refresh 恢复
const auth = createAuth();
const notifications = new NotificationsStore(createNotificationsApi(auth.client));
const settingsApi = createSettingsApi(auth.client);
// 管理面板域 api 与 settingsApi 共用同一 ApiClient（Bearer 与逻辑会话守卫一致）。
const adminApi = createAdminApi(auth.client);
// SettingsProvider 与 Appearance 后续模块共享同一主题控制器，避免重复绑定系统主题监听。
const theme = initTheme();
// 共享壳层单例（fe-shared-shell）：抽屉注册表在首次装配时合成 profile/security 的真实 render。
const drawerRegistry = createDrawerRegistry();

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider store={auth.store}>
        <SettingsProvider
          api={settingsApi}
          authStore={auth.store}
          theme={theme}
          notifications={notifications}
        >
          <AdminProvider api={adminApi}>
            <EscStackProvider>
              <DrawerRegistryProvider registry={drawerRegistry}>
                <NotificationsProvider store={notifications}>
                  <AppRoutes />
                </NotificationsProvider>
              </DrawerRegistryProvider>
            </EscStackProvider>
          </AdminProvider>
        </SettingsProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
