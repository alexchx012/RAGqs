import React from 'react';
import { Link, Navigate } from 'react-router-dom';
import { useAuth } from '../features/auth/AuthContext';

export default function ProtectedRoute({
  children,
  requireAdmin = false,
}: {
  children: React.ReactNode;
  requireAdmin?: boolean;
}) {
  const { status, roles, errorMessage, refresh } = useAuth();

  if (status === 'loading') {
    return <div className="auth-loading" data-testid="auth-loading">加载中...</div>;
  }
  if (status === 'error') {
    return (
      <div className="auth-error" data-testid="auth-error">
        登录态探测失败: {errorMessage}{' '}
        <button type="button" onClick={() => void refresh()}>重试</button>
      </div>
    );
  }
  if (status === 'unauthenticated') {
    return <Navigate to="/login" replace />;
  }
  if (requireAdmin && !roles.includes('super_admin')) {
    return (
      <div className="auth-forbidden" data-testid="auth-forbidden">
        无权限访问项目管理{' '}
        <Link to="/chat">返回聊天</Link>
      </div>
    );
  }
  return <>{children}</>;
}
