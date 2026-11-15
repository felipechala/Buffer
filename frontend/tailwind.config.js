/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    // One type scale: 11 micro / 12 caption / 13 small / 14 body / 16 title / 20 brand
    fontSize: {
      '2xs': ['11px', '16px'], xs: ['12px', '16px'], sm: ['13px', '20px'], base: ['14px', '22px'],
      lg: ['16px', '24px'], xl: ['20px', '28px'],
    },
    extend: {
      fontFamily: {
        brand: ['"Plus Jakarta Sans"', 'Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        // CashGuard: ink navy + blue accent. Ledgerly keeps its own violet (utility `violet-*`).
        cg: { ink: '#0b1b2e', ink2: '#13263d', accent: '#2563eb', accentSoft: '#dbeafe' },
        brand: { DEFAULT: '#4840FF', soft: '#eceaff', ink: '#2f27d6' }, // Buffer indigo (demos)
        canvas: '#f3f5f8',
        // semantic decisions; fg values pass 4.5:1 on their soft bg
        allow: { DEFAULT: '#047857', soft: '#ecfdf5', line: '#a7f3d0', ink: '#065f46' },
        warn: { DEFAULT: '#b45309', soft: '#fffbeb', line: '#fcd34d', ink: '#92400e' },
        block: { DEFAULT: '#b91c1c', soft: '#fef2f2', line: '#fca5a5', ink: '#991b1b' },
      },
    },
  },
  plugins: [],
}
