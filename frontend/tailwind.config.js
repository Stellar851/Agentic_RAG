/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{vue,js,ts,jsx,tsx}'
  ],
  theme: {
    extend: {
      colors: {
        canvas: '#FBFBFA',
        surface: '#FFFFFF',
        subtle: '#F4F4F1',
        ink: '#1F2937',
        muted: '#6B7280',
        line: '#E5E7EB',
        brand: {
          DEFAULT: '#4F46E5',
          hover: '#4338CA',
          soft: '#EEF2FF'
        }
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans"', '"Noto Sans SC"', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'Consolas', 'Monaco', 'monospace']
      },
      boxShadow: {
        card: '0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 12px rgba(15, 23, 42, 0.03)',
        float: '0 8px 28px -6px rgba(15, 23, 42, 0.08), 0 2px 8px -2px rgba(15, 23, 42, 0.04)'
      }
    }
  },
  plugins: []
}
