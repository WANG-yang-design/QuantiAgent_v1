import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * 全局扫描任务状态(跨页面保持):
 * 在任意页面触发分析后, 切换到其他页面仍显示"分析中"并继续轮询。
 */
export const useScanStore = create(
  persist(
    (set, get) => ({
      taskId: null,        // 当前分析任务
      symbol: "",
      status: null,        // RUNNING/DONE/FAILED
      error: "",
      setTask: (taskId, symbol) => set({ taskId, symbol, status: "RUNNING", error: "" }),
      update: (patch) => set(patch),
      clear: () => set({ taskId: null, symbol: "", status: null, error: "" }),
    }),
    { name: "quantiagent_scan_task" }   // localStorage 持久化, 刷新页面不丢失
    // 注: zustand persist 默认使用 localStorage(跨标签页共享)。
    // 如需每次会话独立的任务状态, 改用 createJSONStorage(() => sessionStorage)
  )
);
