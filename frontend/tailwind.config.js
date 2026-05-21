/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: '#0d1117',
        surface: '#161b22',
        border: '#30363d',
        textMain: '#c9d1d9',
        textMuted: '#8b949e',
        primary: '#238636',
        primaryHover: '#2ea043',
        danger: '#f85149',
        warning: '#d29922',
      }
    },
  },
  plugins: [],
}
