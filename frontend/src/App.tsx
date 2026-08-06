import { BrowserRouter } from 'react-router';
import { AuthProvider } from './auth/AuthProvider';
import { createAuth } from './auth/create-auth';
import { EscStackProvider } from './lib/esc-stack-provider';
import { createNotificationsApi } from './notifications/api';
import { NotificationsProvider } from './notifications/NotificationsProvider';
import { NotificationsStore } from './notifications/store';
import { AppRoutes } from './router/AppRoutes';
import { createDrawerRegistry, DrawerRegistryProvider } from './shell/drawer/DrawerRegistryProvider';

// 认证层单例：随页面加载创建；页面刷新后内存 token 丢失，由 bootstrap 静默 refresh 恢复
const auth = createAuth();
// 共享壳层单例（fe-shared-shell）：抽屉注册表（内置占位模块）与通知轮询层
const drawerRegistry = createDrawerRegistry();
const notifications = new NotificationsStore(createNotificationsApi(auth.client));

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider store={auth.store}>
        <EscStackProvider>
          <DrawerRegistryProvider registry={drawerRegistry}>
            <NotificationsProvider store={notifications}>
              <AppRoutes />
            </NotificationsProvider>
          </DrawerRegistryProvider>
        </EscStackProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
