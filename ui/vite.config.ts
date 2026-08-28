import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/review/" : "/",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/v1": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
  },
}));
