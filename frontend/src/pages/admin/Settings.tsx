import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import { Alert, Badge, Button, Card, Field, Input, Loading, Select } from '../../components/ui'
import type { SettingItem } from '../../types'

const GROUP_META: Record<string, { title: string; description: string }> = {
  ai: {
    title: 'AI models & routing',
    description:
      'Each task picks a model and which provider serves it, so Gemini can run on a Google AI Studio key while Claude comes through OpenRouter.',
  },
  ai2: {
    title: 'Secondary provider',
    description:
      'A second provider with its own API key. Tasks routed to "secondary" above use this one.',
  },
  images: {
    title: 'Clinical image search',
    description:
      'Used to attach illustrative images to questions. Microsoft retired the Bing Search API in August 2025; Google Custom Search allows 100 free image queries per day.',
  },
  osce: {
    title: 'OSCE',
    description:
      'Spoken-answer capture and marking for the clinical circuit. The real exam ' +
      'runs 18 stations; your daily circuit length is set below.',
  },
  email: { title: 'Email notifications', description: 'SMTP details, e.g. your SiteGround mailbox.' },
  exam: { title: 'Exam behaviour', description: 'Defaults applied to new sittings.' },
}

const GROUP_ORDER = ['ai', 'ai2', 'osce', 'images', 'email', 'exam']

export default function Settings() {
  const [items, setItems] = useState<SettingItem[]>([])
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null)
  const [emailResult, setEmailResult] = useState<{ ok: boolean; text: string } | null>(null)
  const [testing, setTesting] = useState(false)

  const load = () => {
    setLoading(true)
    api<{ settings: SettingItem[] }>('/admin/settings')
      .then((data) => {
        setItems(data.settings)
        setDraft(Object.fromEntries(data.settings.map((s) => [s.key, s.value])))
      })
      .catch((err) => setMessage({ tone: 'error', text: err.message }))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const grouped = useMemo(() => {
    const map = new Map<string, SettingItem[]>()
    for (const item of items) {
      const list = map.get(item.group) ?? []
      list.push(item)
      map.set(item.group, list)
    }
    return map
  }, [items])

  const dirty = useMemo(
    () => items.filter((item) => draft[item.key] !== item.value),
    [items, draft],
  )

  const save = async () => {
    setSaving(true)
    setMessage(null)
    try {
      const payload = dirty.map((item) => ({ key: item.key, value: draft[item.key] }))
      const data = await api<{ settings: SettingItem[] }>('/admin/settings', {
        method: 'PUT',
        body: { settings: payload },
      })
      setItems(data.settings)
      setDraft(Object.fromEntries(data.settings.map((s) => [s.key, s.value])))
      setMessage({ tone: 'success', text: `Saved ${payload.length} setting(s).` })
    } catch (err) {
      setMessage({ tone: 'error', text: err instanceof Error ? err.message : 'Save failed' })
    } finally {
      setSaving(false)
    }
  }

  const testEmail = async () => {
    setTesting(true)
    setEmailResult(null)
    try {
      const result = await api<{ to: string; host: string; from_address: string }>(
        '/admin/settings/test-email',
        { method: 'POST', body: {} },
      )
      setEmailResult({
        ok: true,
        text: `Sent via ${result.host} as ${result.from_address} — check ${result.to}.`,
      })
    } catch (err) {
      setEmailResult({ ok: false, text: err instanceof Error ? err.message : 'Test failed' })
    } finally {
      setTesting(false)
    }
  }

  const testConnection = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await api<{ model: string; reply: string; latency_ms: number }>(
        '/admin/settings/test-ai',
        { method: 'POST', body: { task: 'structuring' } },
      )
      setTestResult({
        ok: true,
        text: `${result.model} replied "${result.reply}" in ${result.latency_ms} ms.`,
      })
    } catch (err) {
      setTestResult({ ok: false, text: err instanceof Error ? err.message : 'Test failed' })
    } finally {
      setTesting(false)
    }
  }

  if (loading) return <Loading label="Loading settings…" />

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Settings</h1>
          <p className="mt-1 text-sm text-slate-500">
            Changes take effect immediately — no redeploy needed.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {dirty.length > 0 && <Badge tone="amber">{dirty.length} unsaved</Badge>}
          <Button onClick={save} loading={saving} disabled={dirty.length === 0}>
            Save changes
          </Button>
        </div>
      </div>

      {message && <Alert tone={message.tone === 'success' ? 'success' : 'error'}>{message.text}</Alert>}

      {GROUP_ORDER.filter((g) => grouped.has(g)).map((group) => (
        <Card
          key={group}
          title={GROUP_META[group]?.title ?? group}
          description={GROUP_META[group]?.description}
          actions={
            group === 'ai' ? (
              <Button variant="secondary" size="sm" onClick={testConnection} loading={testing}>
                Test connection
              </Button>
            ) : group === 'email' ? (
              <Button
                variant="secondary"
                size="sm"
                onClick={testEmail}
                loading={testing}
                disabled={dirty.length > 0}
              >
                Send test email
              </Button>
            ) : undefined
          }
        >
          {group === 'ai' && testResult && (
            <div className="mb-4">
              <Alert tone={testResult.ok ? 'success' : 'error'}>{testResult.text}</Alert>
            </div>
          )}
          {group === 'email' && (
            <div className="mb-4 space-y-3">
              {dirty.length > 0 && (
                <Alert tone="warning">Save your changes before sending a test.</Alert>
              )}
              {emailResult && (
                <Alert tone={emailResult.ok ? 'success' : 'error'}>{emailResult.text}</Alert>
              )}
            </div>
          )}
          <div className="grid gap-4 sm:grid-cols-2">
            {(grouped.get(group) ?? []).map((item) => (
              <SettingControl
                key={item.key}
                item={item}
                value={draft[item.key]}
                onChange={(value) => setDraft((prev) => ({ ...prev, [item.key]: value }))}
              />
            ))}
          </div>
        </Card>
      ))}
    </div>
  )
}

function SettingControl({
  item,
  value,
  onChange,
}: {
  item: SettingItem
  value: unknown
  onChange: (value: unknown) => void
}) {
  const hint = item.help_text || undefined

  if (typeof item.value === 'boolean') {
    return (
      <div className="flex items-start gap-3 rounded-lg border border-slate-200 p-3">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-1 h-4 w-4 rounded border-slate-300 text-clinical-600 focus:ring-clinical-500"
        />
        <div>
          <p className="text-sm font-medium text-slate-700">{item.label}</p>
          {hint && <p className="mt-0.5 text-xs text-slate-500">{hint}</p>}
        </div>
      </div>
    )
  }

  if (item.choices.length > 0) {
    return (
      <Field label={item.label} hint={hint}>
        <Select value={String(value ?? '')} onChange={(e) => onChange(e.target.value)}>
          {item.choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </Select>
      </Field>
    )
  }

  if (item.is_secret) {
    return (
      <Field
        label={item.label}
        hint={item.is_set ? 'A value is stored. Type a new one to replace it.' : hint}
      >
        <Input
          type="password"
          value={String(value ?? '')}
          placeholder={item.is_set ? String(item.value) : 'Not set'}
          onChange={(e) => onChange(e.target.value)}
          autoComplete="off"
        />
      </Field>
    )
  }

  const numeric = typeof item.value === 'number'
  return (
    <Field label={item.label} hint={hint}>
      <Input
        type={numeric ? 'number' : 'text'}
        step={numeric ? 'any' : undefined}
        value={String(value ?? '')}
        onChange={(e) => onChange(numeric ? Number(e.target.value) : e.target.value)}
      />
    </Field>
  )
}
