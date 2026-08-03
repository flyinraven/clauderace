import { useState } from 'react'
import { useAuth } from '../auth/AuthContext'
import { Alert, Button, Field, Input } from '../components/ui'

type Mode = 'login' | 'invite'

// The link in an invite email carries the code, so someone arriving from their
// invitation lands on the sign-up form with it already filled in. The query
// sits inside the hash (#/login?invite=...) because the app uses HashRouter.
function inviteCodeFromLink(): string {
  const hash = window.location.hash
  const query = hash.includes('?')
    ? hash.slice(hash.indexOf('?') + 1)
    : window.location.search
  return new URLSearchParams(query).get('invite')?.toUpperCase() ?? ''
}

const codeFromLink = inviteCodeFromLink()

export default function Login() {
  const { login, redeemInvite, serverWaking, authError } = useAuth()
  const [mode, setMode] = useState<Mode>(codeFromLink ? 'invite' : 'login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [code, setCode] = useState(codeFromLink)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      if (mode === 'login') {
        await login(email, password)
      } else {
        await redeemInvite({ code, email, full_name: fullName || undefined, password })
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-12">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <span className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-clinical-600 text-lg font-bold text-white">
            R
          </span>
          <h1 className="text-xl font-semibold text-slate-900">RACE Exam Simulator</h1>
          <p className="mt-1 text-sm text-slate-500">
            RANZCO Advanced Clinical Examination preparation
          </p>
        </div>

        <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="mb-5 flex rounded-lg bg-slate-100 p-1">
            {(['login', 'invite'] as Mode[]).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => {
                  setMode(value)
                  setError(null)
                }}
                className={
                  'flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition ' +
                  (mode === value ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-600')
                }
              >
                {value === 'login' ? 'Sign in' : 'Use an invite code'}
              </button>
            ))}
          </div>

          <form onSubmit={submit} className="space-y-4">
            {mode === 'invite' && (
              <Field label="Invite code" hint="Supplied by your administrator, e.g. ABCD-EFGH-JKLM">
                <Input
                  value={code}
                  onChange={(e) => setCode(e.target.value.toUpperCase())}
                  placeholder="ABCD-EFGH-JKLM"
                  autoComplete="off"
                  required
                />
              </Field>
            )}

            <Field label="Email address">
              <Input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </Field>

            {mode === 'invite' && (
              <Field label="Full name">
                <Input value={fullName} onChange={(e) => setFullName(e.target.value)} autoComplete="name" />
              </Field>
            )}

            <Field
              label="Password"
              hint={mode === 'invite' ? 'At least 10 characters.' : undefined}
            >
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                minLength={mode === 'invite' ? 10 : undefined}
                required
              />
            </Field>

            {error && <Alert tone="error">{error}</Alert>}
            {!error && authError && <Alert tone="warning">{authError}</Alert>}

            {serverWaking && (
              <Alert tone="warning" title="Waking the server">
                The free hosting tier sleeps when idle. The first request of the day takes about a minute.
              </Alert>
            )}

            <Button type="submit" loading={busy} className="w-full">
              {mode === 'login' ? 'Sign in' : 'Create my account'}
            </Button>
          </form>
        </div>

        <p className="mt-4 text-center text-xs text-slate-400">
          Access is by invitation only.
        </p>
      </div>
    </div>
  )
}
