import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/** v30 Apex — production bundle only; no dev proxy / browser hooks. */
export default defineConfig({
  base: "./",
  mode: "production",
  plugins: [
    react({
      fastRefresh: false,
    }),
    tailwindcss(),
  ],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    minify: "terser",
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
  },
});
