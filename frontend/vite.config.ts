import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // The built application and APIs share one origin; no CORS or browser keys.
  build: { outDir: "../src/agentpal/web", emptyOutDir: true },
});
