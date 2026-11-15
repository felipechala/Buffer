// The Buffer "B" mark: two stacked, right-leaning rounded pieces split by a thin diagonal gap.
// tone 'light' = for light backgrounds (navy lower piece); 'dark' = for dark backgrounds (white lower piece).
export const BUFFER_INDIGO = '#4840FF'
export const BUFFER_NAVY = '#0A0E33'

export function BufferMark({ size = 24, tone = 'light', className, title }: {
  size?: number; tone?: 'light' | 'dark'; className?: string; title?: string
}) {
  const lower = tone === 'dark' ? '#ffffff' : BUFFER_NAVY
  return (
    <svg width={size * 1.05} height={size} viewBox="0 6 252 246" className={className}
      role={title ? 'img' : undefined} aria-label={title} aria-hidden={title ? undefined : true}>
      <path d="M60 9H188C218 9 240 28 240 55C240 78 218 99 195 103L20 147Z" fill={BUFFER_INDIGO} />
      <path d="M32 172C36 163 44 159 58 156L198 123C232 117 250 148 250 177C250 212 225 249 168 249H8Z" fill={lower} />
    </svg>
  )
}

export function BufferWordmark({ size = 28, tone = 'light', className = '' }: {
  size?: number; tone?: 'light' | 'dark'; className?: string
}) {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <BufferMark size={size} tone={tone} />
      <span className={`font-brand font-extrabold leading-none tracking-tight ${tone === 'dark' ? 'text-white' : 'text-[#0A0E33]'}`}
        style={{ fontSize: size * 0.95 }}>Buffer</span>
    </span>
  )
}
