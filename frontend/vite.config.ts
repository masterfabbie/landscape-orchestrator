// Builds the browser interface into the Python package, where the server delivers it.
// File names have no hash: the page adds ?v=<content hash> itself, and a stable name keeps
// the committed build easy to compare in the CI.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  base: "/static/app/",
  build: {
    outDir: "../src/landscape_orchestrator/web/static/app",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: { landscape: "src/landscape/main.tsx" },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name][extname]",
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
