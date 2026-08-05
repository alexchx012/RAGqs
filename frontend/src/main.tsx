import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { copy } from './copy';
import { initTheme } from './theme/theme';
import './index.css';

initTheme();
document.title = copy.appName;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
