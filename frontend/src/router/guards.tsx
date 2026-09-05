/*
 * 路由守卫（规格 §4）。
 * - RequireAuth：未认证访问业务页 → 重定向 /login（携带原路径供登录后回跳）；认证状态未知（静默 refresh 进行中）时等待。
 * - RedirectIfAuthenticated：已认证访问 /login → 深链回跳（经 parseDrawerLocation 校验）或按角色重定向落地页。
 * 鉴权以后端为准，前端按角色隐藏模块不构成安全边界。
 */

import { Navigate, Outlet, useLocation } from 'react-router';
import { useAuthState } from '../auth/AuthProvider';
import { FullPageLoading } from '../shell/FullPageLoading';
import { parseDrawerLocation } from './drawer-params';
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
  const location = useLocation();
  if (state.status === 'unknown') {
    return <FullPageLoading />;
  }
  if (state.status === 'authenticated' && state.user !== null) {
    // 深链回跳（审查 P1#12）：RequireAuth 送到 /login 的 from 若为抽屉路径（/settings、/admin
    // 路径段）则回原目标；parseDrawerLocation 校验形状，非抽屉路径（含 '/'、未知段）回角色
    // 落地页。未注册层与管理段权限由 DrawerHost 现有守卫承担（占位 / 弹回主页）。
    const from = (location.state as { from?: unknown } | null)?.from;
    if (typeof from === 'string' && parseDrawerLocation(from).open) {
      return <Navigate to={from} replace />;
    }
    const target = landingTargetFor(state.user.role);
    return <Navigate to={target.path} replace state={target.state} />;
  }
  return <Outlet />;
}
