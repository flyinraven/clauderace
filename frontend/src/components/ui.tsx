import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

// --- Button ---------------------------------------------------------------
type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
  loading?: boolean
}

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  className,
  children,
  disabled,
  ...rest
}: ButtonProps) {
  const base =
    'inline-flex items-center justify-center gap-2 rounded-lg font-medium transition ' +
    'focus:outline-none focus-visible:ring-2 focus-visible:ring-clinical-500 focus-visible:ring-offset-2 ' +
    'disabled:cursor-not-allowed disabled:opacity-50'
  const sizes = { sm: 'px-3 py-1.5 text-sm', md: 'px-4 py-2 text-sm' }
  const variants = {
    primary: 'bg-clinical-600 text-white hover:bg-clinical-700 shadow-sm',
    secondary: 'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50 shadow-sm',
    ghost: 'text-slate-600 hover:bg-slate-100',
    danger: 'bg-red-600 text-white hover:bg-red-700 shadow-sm',
  }
  return (
    <button
      className={cx(base, sizes[size], variants[variant], className)}
      disabled={disabled || loading}
      {...rest}
    >
      {loading && <Spinner className="h-4 w-4" />}
      {children}
    </button>
  )
}

export function Spinner({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <svg className={cx('animate-spin', className)} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
      />
    </svg>
  )
}

// --- Surfaces -------------------------------------------------------------
export function Card({
  title,
  description,
  actions,
  children,
  className,
}: {
  title?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cx('rounded-xl border border-slate-200 bg-white shadow-sm', className)}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div>
            {title && <h2 className="text-base font-semibold text-slate-900">{title}</h2>}
            {description && <p className="mt-1 text-sm text-slate-500">{description}</p>}
          </div>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="px-5 py-4">{children}</div>
    </section>
  )
}

export function Badge({
  children,
  tone = 'slate',
}: {
  children: ReactNode
  tone?: 'slate' | 'green' | 'amber' | 'red' | 'blue' | 'violet'
}) {
  const tones = {
    slate: 'bg-slate-100 text-slate-700 ring-slate-200',
    green: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    amber: 'bg-amber-50 text-amber-800 ring-amber-200',
    red: 'bg-red-50 text-red-700 ring-red-200',
    blue: 'bg-clinical-50 text-clinical-800 ring-clinical-200',
    violet: 'bg-violet-50 text-violet-700 ring-violet-200',
  }
  return (
    <span
      className={cx(
        'inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset',
        tones[tone],
      )}
    >
      {children}
    </span>
  )
}

export function Alert({
  tone = 'info',
  title,
  children,
}: {
  tone?: 'info' | 'success' | 'warning' | 'error'
  title?: ReactNode
  children?: ReactNode
}) {
  const tones = {
    info: 'border-clinical-200 bg-clinical-50 text-clinical-900',
    success: 'border-emerald-200 bg-emerald-50 text-emerald-900',
    warning: 'border-amber-200 bg-amber-50 text-amber-900',
    error: 'border-red-200 bg-red-50 text-red-900',
  }
  return (
    <div className={cx('rounded-lg border px-4 py-3 text-sm', tones[tone])} role="status">
      {title && <p className="font-semibold">{title}</p>}
      {children && <div className={title ? 'mt-1' : undefined}>{children}</div>}
    </div>
  )
}

// --- Form controls --------------------------------------------------------
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="block text-sm font-medium text-slate-700">{label}</span>
      {children}
      {hint && !error && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
      {error && <span className="mt-1 block text-xs text-red-600">{error}</span>}
    </label>
  )
}

const controlClass =
  'mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm ' +
  'placeholder:text-slate-400 focus:border-clinical-500 focus:outline-none focus:ring-1 focus:ring-clinical-500 ' +
  'disabled:bg-slate-50 disabled:text-slate-500'

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cx(controlClass, className)} {...rest} />
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cx(controlClass, 'font-mono', className)} {...rest} />
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cx(controlClass, className)} {...rest}>
      {children}
    </select>
  )
}

// --- Pagination -----------------------------------------------------------
/**
 * Page through a long list.
 *
 * Numbered rather than next-and-back alone: eighteen stations a sitting means
 * the one you want is a known distance in, and stepping there one page at a
 * time is the thing worth avoiding. Long runs collapse to an ellipsis so the
 * control stays one line however many pages there are.
 */
export function Pagination({
  total,
  pageSize,
  offset,
  onOffset,
  noun = 'item',
}: {
  total: number
  pageSize: number
  offset: number
  onOffset: (offset: number) => void
  noun?: string
}) {
  if (total <= pageSize) return null

  const pageCount = Math.ceil(total / pageSize)
  const current = Math.floor(offset / pageSize) + 1

  // First, last, and a window around the current page — but eliding one or two
  // numbers out of a handful buys no space and hides pages you can reach, so
  // short runs are always shown in full.
  const ALWAYS_SHOW_UP_TO = 7
  const pages: (number | 'gap')[] = []
  for (let page = 1; page <= pageCount; page += 1) {
    const near = Math.abs(page - current) <= 1
    if (pageCount <= ALWAYS_SHOW_UP_TO || page === 1 || page === pageCount || near) {
      pages.push(page)
    } else if (pages[pages.length - 1] !== 'gap') {
      pages.push('gap')
    }
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 pt-3">
      <p className="text-sm text-slate-500">
        Showing {offset + 1}–{Math.min(offset + pageSize, total)} of {total} {noun}
        {total === 1 ? '' : 's'}
      </p>
      <div className="flex flex-wrap items-center gap-1">
        <Button
          variant="secondary"
          size="sm"
          disabled={current === 1}
          onClick={() => onOffset(Math.max(0, offset - pageSize))}
        >
          Previous
        </Button>
        {pages.map((page, index) =>
          page === 'gap' ? (
            <span key={`gap-${index}`} className="px-1.5 text-sm text-slate-400">
              …
            </span>
          ) : (
            <button
              key={page}
              type="button"
              onClick={() => onOffset((page - 1) * pageSize)}
              aria-current={page === current ? 'page' : undefined}
              className={cx(
                'min-w-[2rem] rounded-lg px-2 py-1.5 text-sm font-medium transition',
                page === current
                  ? 'bg-clinical-600 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200',
              )}
            >
              {page}
            </button>
          ),
        )}
        <Button
          variant="secondary"
          size="sm"
          disabled={current === pageCount}
          onClick={() => onOffset(offset + pageSize)}
        >
          Next
        </Button>
      </div>
    </div>
  )
}

// --- States ---------------------------------------------------------------
export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-700">{title}</p>
      {children && <div className="mt-1 text-sm text-slate-500">{children}</div>}
    </div>
  )
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-10 text-sm text-slate-500">
      <Spinner className="h-5 w-5 text-clinical-600" />
      {label}
    </div>
  )
}

export function ProgressBar({ value, label }: { value: number; label?: ReactNode }) {
  const percent = Math.round(Math.min(1, Math.max(0, value)) * 100)
  return (
    <div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200">
        <div
          className="h-full rounded-full bg-clinical-600 transition-[width] duration-500"
          style={{ width: `${percent}%` }}
        />
      </div>
      {label && <p className="mt-1.5 text-xs text-slate-500">{label}</p>}
    </div>
  )
}

/** A row of tabs, for a page holding two things that were crowding each other. */
export function Tabs<T extends string>({
  value,
  onChange,
  tabs,
}: {
  value: T
  onChange: (value: T) => void
  tabs: { value: T; label: string; count?: number }[]
}) {
  return (
    <div className="flex gap-1 border-b border-slate-200" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          aria-selected={value === tab.value}
          onClick={() => onChange(tab.value)}
          className={cx(
            'rounded-t-lg px-4 py-2 text-sm font-medium transition',
            value === tab.value
              ? 'border-b-2 border-clinical-600 text-clinical-700'
              : 'border-b-2 border-transparent text-slate-500 hover:text-slate-700',
          )}
        >
          {tab.label}
          {tab.count != null && (
            <span className="ml-2 text-xs text-slate-400">{tab.count}</span>
          )}
        </button>
      ))}
    </div>
  )
}
