import { useEffect, useRef } from "react";
import * as echarts from "echarts";

/**
 * ECharts K线图: 蜡烛图 + MA5/20/60 + 成交量副图
 * props: candles = [{date, open, high, low, close, volume, ma5, ma20, ma60}]
 * 右侧Y轴显示相对昨收的涨跌百分比(修复: 原只有价格轴, 看不到涨跌幅)
 */
const fmtVol = (v) => {
  if (v == null) return "-";
  const a = Math.abs(v);
  if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (a >= 1e4) return (v / 1e4).toFixed(1) + "万";
  return String(Math.round(v));
};

export default function KlineChart({ candles, height = 420 }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !candles?.length) return;
    if (!chartRef.current) chartRef.current = echarts.init(ref.current);

    const dates = candles.map((c) => c.date);
    // 当日实时K线(收盘价已用实时行情刷新): 高亮描边 + 涨跌色
    const liveIndex = candles.map((c) => c.is_live).lastIndexOf(true);
    const kdata = candles.map((c) => [c.open, c.close, c.low, c.high]);
    const vols = candles.map((c) => ({ value: c.volume, itemStyle: { color: c.close >= c.open ? "#e03131" : "#2f9e44" } }));
    const mk = (key, color) => candles.map((c) => ({ value: c[key], itemStyle: { color } }));

    // 涨跌百分比基准: 最后一根非实时K线的收盘价(≈昨收)
    let base = 0;
    for (let i = candles.length - 1; i >= 0; i--) {
      if (!candles[i].is_live) { base = candles[i].close; break; }
    }
    if (!base) base = candles[0].close || 1;

    chartRef.current.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          const c = candles[i];
          const chg = base ? ((c.close / base - 1) * 100).toFixed(2) : "-";
          return `${c.date}${c.is_live ? " (实时)" : ""}<br/>开 ${c.open} 高 ${c.high}<br/>低 ${c.low} 收 ${c.close} (${chg}%)<br/>量 ${fmtVol(c.volume)}`;
        },
      },
      legend: { data: ["K线", "MA5", "MA20", "MA60"], top: 0, textStyle: { fontSize: 11 } },
      grid: [
        { left: 60, right: 60, top: 28, height: "55%" },
        { left: 60, right: 60, top: "72%", height: "18%" },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLabel: { fontSize: 10 } },
        { type: "category", data: dates, gridIndex: 1, axisLabel: { show: false } },
      ],
      yAxis: [
        { gridIndex: 0, scale: true, splitLine: { lineStyle: { opacity: 0.3 } } },
        {
          gridIndex: 0, scale: true, position: "right", splitLine: { show: false },
          axisLabel: { fontSize: 10, formatter: (v) => `${((v / base - 1) * 100).toFixed(1)}%` },
        },
        { gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 40, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 16 },
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: kdata,
          itemStyle: { color: "#e03131", color0: "#2f9e44", borderColor: "#e03131", borderColor0: "#2f9e44" },
          markPoint: liveIndex >= 0 ? {
            symbol: "pin",
            symbolSize: 38,
            itemStyle: { color: "#f59f00" },
            label: { fontSize: 9, color: "#fff", formatter: "实时" },
            data: [{ coord: [liveIndex, candles[liveIndex].high] }],
          } : undefined,
        },
        { name: "MA5", type: "line", data: mk("ma5", "#1971c2"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "MA20", type: "line", data: mk("ma20", "#f59f00"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "MA60", type: "line", data: mk("ma60", "#9c36b5"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        // 隐藏绑定系列: 让右侧涨跌百分比轴被引用(修复: ECharts 不被任何 series
        // 引用的坐标轴不会渲染, 之前右侧 % 轴看不见)
        { name: "涨跌%", type: "line", data: candles.map((c) => c.close), xAxisIndex: 0, yAxisIndex: 1,
          showSymbol: false, silent: true, legendHoverLink: false,
          lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 }, tooltip: { show: false } },
        { name: "成交量", type: "bar", data: vols, xAxisIndex: 1, yAxisIndex: 2 },
      ],
    });
    const onResize = () => chartRef.current?.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      // 修复: SPA 反复切换页面/标的时 ECharts 实例泄漏(含 dataZoom/resize 监听)
      if (chartRef.current) {
        chartRef.current.dispose();
        chartRef.current = null;
      }
    };
  }, [candles]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
