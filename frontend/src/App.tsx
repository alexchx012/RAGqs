import { BrowserRouter } from 'react-router';
import { AuthProvider } from './auth/AuthProvider';
import { createAuth } from './auth/create-auth';
import { AppRoutes } from './router/AppRoutes';

// 认证层单例：随页面加载创建；页面刷新后内存 token 丢失，由 bootstrap 静默 refresh 恢复
const auth = createAuth();

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider store={auth.store}>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
