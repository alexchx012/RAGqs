/*
 * 路由守卫（规格 §4）。
 * - RequireAuth：未认证访问业务页 → 重定向 /login；认证状态未知（静默 refresh 进行中）时等待。
 * - RedirectIfAuthenticated：已认证访问 /login → 按角色重定向落地页。
 * 鉴权以后端为准，前端按角色隐藏模块不构成安全边界。
 */

import { Navigate, Outlet, useLocation } from 'react-router';
import { useAuthState } from '../auth/AuthProvider';
import { FullPageLoading } from '../shell/FullPageLoading';
import { landingTargetFor } from './landing';

export function RequireAuth() {
  const state = useAuthState();
  const location = useLocation();
  if (state.status === 'unknown') {
    return <FullPageLoading />;
  }
  if (state.status === 'unauthenticated') {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <Outlet />;
}

export function RedirectIfAuthenticated() {
  const state = useAuthState();
  if (state.status === 'unknown') {
    return <FullPageLoading />;
  }
  if (state.status === 'authenticated' && state.user !== null) {
    const target = landingTargetFor(state.user.role);
    return <Navigate to={target.path} replace state={target.state} />;
  }
  return <Outlet />;
}
