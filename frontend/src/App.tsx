import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from './features/auth/AuthContext';
import LoginPage from './features/auth/LoginPage';
import ProtectedRoute from './routes/ProtectedRoute';
import RootRedirect from './routes/RootRedirect';
import AppNav from './routes/AppNav';
import ChatPage from './pages/ChatPage';
import KnowledgePage from './pages/KnowledgePage';
import AdminProjectsPage from './pages/AdminProjectsPage';

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="app-shell">
      <AppNav />
      {children}
    </div>
  );
}

/** Route tree without BrowserRouter — used by App and by tests with MemoryRouter. */
export function AppRoutes() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/chat"
          element={
            <ProtectedRoute>
              <Shell>
                <ChatPage />
              </Shell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/knowledge"
          element={
            <ProtectedRoute>
              <Shell>
                <KnowledgePage />
              </Shell>
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin/projects"
          element={
            <ProtectedRoute requireAdmin>
              <Shell>
                <AdminProjectsPage />
              </Shell>
            </ProtectedRoute>
          }
        />
        <Route path="/" element={<RootRedirect />} />
        <Route path="*" element={<RootRedirect />} />
      </Routes>
    </AuthProvider>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}
