import React from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from '../features/auth/AuthContext';

export default function RootRedirect() {
  const { status, errorMessage, refresh } = useAuth();

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
  if (status === 'authenticated') {
    return <Navigate to="/chat" replace />;
  }
  return <Navigate to="/login" replace />;
}
