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
    rollupOptions: {
      output: {
        // 第三方库分包: echarts/recharts 体积大, 拆出独立 chunk 提高首屏加载
        manualChunks: {
          echarts: ["echarts"],
          recharts: ["recharts"],
          vendor: ["react", "react-dom", "react-router-dom", "axios", "@tanstack/react-query"],
        },
      },
    },
  },
});
