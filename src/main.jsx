import React from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import AppShell from './AppShell.jsx';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AppShell />
  </React.StrictMode>
);
