import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import App from './App'
import { AuthProvider } from './auth/AuthContext'
import './index.css'

// HashRouter rather than BrowserRouter because SiteGround's nginx serves this
// document root directly and returns 403 for any path that is not a real file,
// before Apache (and therefore .htaccess) ever sees the request. Disabling
// NGINX Direct Delivery did not stop it - responses still carry
// `x-proxy-cache-info: DT:1` with no `x-httpd-modphp`, meaning nginx answered.
// Everything after the '#' is never sent to the server, so routing works
// regardless of host configuration.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <HashRouter>
      <AuthProvider>
        <App />
      </AuthProvider>
    </HashRouter>
  </StrictMode>,
)
