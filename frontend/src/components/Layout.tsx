import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { Badge, Button, cx } from './ui'

const studentLinks = [
  { to: '/', label: 'Dashboard', end: true },
  { to: '/exams', label: 'Written' },
  { to: '/osce', label: 'OSCE' },
  { to: '/bank', label: 'Question bank' },
]

const adminLinks = [
  { to: '/admin/documents', label: 'Documents' },
  { to: '/admin/papers', label: 'Papers' },
  { to: '/admin/station-images', label: 'Station images' },
  { to: '/admin/users', label: 'Users' },
  { to: '/admin/settings', label: 'Settings' },
  { to: '/admin/errors', label: 'Errors' },
]

export default function Layout() {
  const { user, logout, serverWaking } = useAuth()

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-clinical-600 text-sm font-bold text-white">
              R
            </span>
            <div className="leading-tight">
              <p className="text-sm font-semibold text-slate-900">RACE Exam Simulator</p>
              <p className="text-xs text-slate-500">RANZCO Advanced Clinical Examination</p>
            </div>
          </div>

          <nav className="flex flex-1 flex-wrap items-center gap-1">
            {studentLinks.map((link) => (
              <NavItem key={link.to} {...link} />
            ))}
            {user?.role === 'admin' && (
              <>
                <span className="mx-2 hidden h-5 w-px bg-slate-200 sm:block" />
                {adminLinks.map((link) => (
                  <NavItem key={link.to} {...link} />
                ))}
              </>
            )}
          </nav>

          <div className="flex items-center gap-3">
            {user?.role === 'admin' && <Badge tone="violet">Admin</Badge>}
            <span className="hidden text-sm text-slate-600 sm:inline">{user?.email}</span>
            <Button variant="ghost" size="sm" onClick={logout}>
              Sign out
            </Button>
          </div>
        </div>

        {serverWaking && (
          <div className="bg-amber-50 px-4 py-1.5 text-center text-xs text-amber-800">
            Waking the server — the free hosting tier sleeps when idle, so this first request takes about a minute.
          </div>
        )}
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6">
        <Outlet />
      </main>

      <footer className="border-t border-slate-200 bg-white px-4 py-3 text-center text-xs text-slate-400">
        Private study tool. Past paper content is © RANZCO and must not be redistributed.
      </footer>
    </div>
  )
}

function NavItem({ to, label, end }: { to: string; label: string; end?: boolean }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        cx(
          'rounded-lg px-3 py-1.5 text-sm font-medium transition',
          isActive ? 'bg-clinical-50 text-clinical-800' : 'text-slate-600 hover:bg-slate-100',
        )
      }
    >
      {label}
    </NavLink>
  )
}
