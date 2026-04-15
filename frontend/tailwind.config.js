/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        /** 暖钛灰底，与 index.css 同步 */
        'ti-void': '#1a1816',
        'ti-panel': '#222018',
      },
      backdropBlur: {
        /** 与毛玻璃面板统一 */
        glass: '12px',
      },
    },
  },
};
