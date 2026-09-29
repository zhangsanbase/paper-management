import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 后端端口与 backend/server_config.py 共用同一个环境变量 PAPER_MANAGER_PORT。
// tsconfig 的 include 只有 src，本文件不参与 tsc 检查，所以这里显式声明 process，
// 免得编辑器因为没有 @types/node 而报错。
declare const process: { env: Record<string, string | undefined> };
const backendPort = process.env.PAPER_MANAGER_PORT ?? "8765";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "frontend/dist",
    emptyOutDir: true
  },
  server: {
    proxy: {
      "/api": `http://127.0.0.1:${backendPort}`
    }
  }
});
