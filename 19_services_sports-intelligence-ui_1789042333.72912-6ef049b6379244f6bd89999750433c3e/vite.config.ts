import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    allowedHosts: [
      ".ngrok-free.app",
      ".ngrok.app",
      "a60a-115-97-186-115.ngrok-free.app",
    ],
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
