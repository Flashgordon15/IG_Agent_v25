/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0d1117",
        surface: "#161b22",
        border: "#30363d",
        green: "#3fb950",
        red: "#f85149",
        amber: "#d29922",
        blue: "#388bfd",
        muted: "#8b949e",
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
