import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { Alert, Badge, Button, Card, EmptyState, Field, Input, Loading, Select } from '../../components/ui'
import type { Invite, Role, User } from '../../types'

export default function Users() {
  const [users, setUsers] = useState<User[]>([])
  const [invites, setInvites] = useState<Invite[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState<Role>('student')
  const [creating, setCreating] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)

  const load = () => {
    Promise.all([api<User[]>('/admin/users'), api<Invite[]>('/admin/invites')])
      .then(([u, i]) => {
        setUsers(u)
        setInvites(i)
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const createInvite = async (event: React.FormEvent) => {
    event.preventDefault()
    setCreating(true)
    setError(null)
    try {
      await api<Invite>('/admin/invites', {
        method: 'POST',
        body: { email: inviteEmail.trim() || null, role: inviteRole },
      })
      setInviteEmail('')
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create invite')
    } finally {
      setCreating(false)
    }
  }

  const updateUser = async (user: User, changes: Partial<Pick<User, 'role' | 'is_active'>>) => {
    setError(null)
    try {
      await api(`/admin/users/${user.id}`, { method: 'PATCH', body: changes })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Update failed')
    }
  }

  const revokeInvite = async (invite: Invite) => {
    try {
      await api(`/admin/invites/${invite.id}`, { method: 'DELETE' })
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not revoke invite')
    }
  }

  const copy = async (code: string) => {
    await navigator.clipboard.writeText(code)
    setCopied(code)
    setTimeout(() => setCopied(null), 2000)
  }

  if (loading) return <Loading label="Loading users…" />

  const pending = invites.filter((i) => !i.used_at)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Users &amp; invitations</h1>
        <p className="mt-1 text-sm text-slate-500">
          There is no public signup. Issue an invite code and send it to the trainee.
        </p>
      </div>

      {error && <Alert tone="error">{error}</Alert>}

      <Card title="Issue an invite">
        <form onSubmit={createInvite} className="grid gap-4 sm:grid-cols-[2fr_1fr_auto] sm:items-end">
          <Field label="Email (optional)" hint="If set, only this address can redeem the code.">
            <Input
              type="email"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              placeholder="trainee@example.com"
            />
          </Field>
          <Field label="Role">
            <Select value={inviteRole} onChange={(e) => setInviteRole(e.target.value as Role)}>
              <option value="student">Student</option>
              <option value="admin">Admin</option>
            </Select>
          </Field>
          <Button type="submit" loading={creating}>
            Create invite
          </Button>
        </form>

        {pending.length > 0 && (
          <div className="mt-5 space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Unused codes
            </p>
            {pending.map((invite) => (
              <div
                key={invite.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 px-3 py-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <code className="rounded bg-slate-100 px-2 py-1 font-mono text-sm">{invite.code}</code>
                  <Badge tone={invite.role === 'admin' ? 'violet' : 'slate'}>{invite.role}</Badge>
                  {invite.email && <span className="text-xs text-slate-500">for {invite.email}</span>}
                </div>
                <div className="flex items-center gap-1">
                  <Button variant="ghost" size="sm" onClick={() => copy(invite.code)}>
                    {copied === invite.code ? 'Copied' : 'Copy'}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => revokeInvite(invite)}>
                    Revoke
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Accounts">
        {users.length === 0 ? (
          <EmptyState title="No accounts yet" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="pb-2 font-medium">User</th>
                  <th className="pb-2 font-medium">Role</th>
                  <th className="pb-2 font-medium">Last sign-in</th>
                  <th className="pb-2 font-medium">Status</th>
                  <th className="pb-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {users.map((user) => (
                  <tr key={user.id}>
                    <td className="py-3 pr-3">
                      <p className="font-medium text-slate-800">{user.full_name ?? '—'}</p>
                      <p className="text-xs text-slate-500">{user.email}</p>
                    </td>
                    <td className="py-3 pr-3">
                      <Select
                        className="mt-0 w-32 py-1 text-xs"
                        value={user.role}
                        onChange={(e) => updateUser(user, { role: e.target.value as Role })}
                      >
                        <option value="student">Student</option>
                        <option value="admin">Admin</option>
                      </Select>
                    </td>
                    <td className="py-3 pr-3 text-slate-600">
                      {user.last_login_at ? new Date(user.last_login_at).toLocaleString('en-AU') : 'Never'}
                    </td>
                    <td className="py-3 pr-3">
                      <Badge tone={user.is_active ? 'green' : 'red'}>
                        {user.is_active ? 'Active' : 'Disabled'}
                      </Badge>
                    </td>
                    <td className="py-3 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => updateUser(user, { is_active: !user.is_active })}
                      >
                        {user.is_active ? 'Disable' : 'Enable'}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
