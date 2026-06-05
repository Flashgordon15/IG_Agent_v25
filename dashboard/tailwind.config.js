/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0f1117",
        card: "#1e2432",
        accent: "#2e75b6",
        foreground: "#e2e8f0",
        success: "#22c55e",
        warning: "#f59e0b",
        danger: "#ef4444",
        surface: "#1e2432",
        border: "#2a3344",
        muted: "#94a3b8",
      },
      fontFamily: {
        sans: [
          "system-ui",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "sans-serif",
        ],
      },
      fontSize: {
        body: "13px",
        label: "10px",
        price: "22px",
      },
    },
  },
  plugins: [],
};
