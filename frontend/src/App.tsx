import { BrowserRouter } from 'react-router';
import { AppRoutes } from './router/AppRoutes';

export function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}
