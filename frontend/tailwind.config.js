/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: '#050506',
        surface: '#0e0e11',
        border: '#1e1e22',
        textMain: '#ffffff',
        textMuted: '#9ca3af',
        primary: '#ff7a30',
        primaryHover: '#e0631f',
        danger: '#f85149',
        warning: '#d29922',
      }
    },
  },
  plugins: [],
}
