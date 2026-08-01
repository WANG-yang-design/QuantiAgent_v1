import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物输出到 dist, 由 FastAPI 静态托管
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
  },
});
