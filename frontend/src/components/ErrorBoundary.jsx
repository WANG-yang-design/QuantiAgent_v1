import React from "react";

/**
 * 全局错误边界: React 渲染期异常会卸载整棵 root 导致白屏且无提示。
 * 兜底显示错误信息 + 重试按钮(修复: 原实现无任何错误边界)。
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    console.error("页面渲染异常:", error, info);
  }

  render() {
    if (this.state.hasError) {
      // 局部 fallback(如图表卡片): 不遮挡整页, 只降级该区域
      if (this.props.fallback) {
        return this.props.fallback(this.state.error, () => this.setState({ hasError: false, error: null }));
      }
      return (
        <div className="min-h-screen flex items-center justify-center bg-gray-100 p-6">
          <div className="card max-w-lg w-full text-center">
            <div className="text-3xl mb-2">⚠️</div>
            <h1 className="text-lg font-bold text-gray-800 mb-2">页面出现异常</h1>
            <p className="text-sm text-gray-500 mb-4 break-all">
              {this.state.error?.message || "未知错误"}
            </p>
            <div className="flex gap-2 justify-center">
              <button
                className="btn-primary"
                onClick={() => window.location.reload()}
              >
                刷新页面
              </button>
              <button
                className="btn-ghost"
                onClick={() => this.setState({ hasError: false, error: null })}
              >
                尝试恢复
              </button>
            </div>
            <p className="text-xs text-gray-400 mt-4">
              如反复出现, 请检查后端日志(web/api/main.py 全局异常日志)
            </p>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
