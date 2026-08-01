import { useEffect, useRef } from "react";
import * as echarts from "echarts";

/**
 * ECharts K线图: 蜡烛图 + MA5/20/60 + 成交量副图
 * props: candles = [{date, open, high, low, close, volume, ma5, ma20, ma60}]
 */
export default function KlineChart({ candles, height = 420 }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !candles?.length) return;
    if (!chartRef.current) chartRef.current = echarts.init(ref.current);

    const dates = candles.map((c) => c.date);
    const kdata = candles.map((c) => [c.open, c.close, c.low, c.high]);
    const vols = candles.map((c) => ({ value: c.volume, itemStyle: { color: c.close >= c.open ? "#e03131" : "#2f9e44" } }));
    const mk = (key, color) => candles.map((c) => ({ value: c[key], itemStyle: { color } }));

    chartRef.current.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          const c = candles[i];
          return `${c.date}<br/>开 ${c.open} 高 ${c.high}<br/>低 ${c.low} 收 ${c.close}<br/>量 ${c.volume}`;
        },
      },
      legend: { data: ["K线", "MA5", "MA20", "MA60"], top: 0, textStyle: { fontSize: 11 } },
      grid: [
        { left: 60, right: 20, top: 28, height: "55%" },
        { left: 60, right: 20, top: "72%", height: "18%" },
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLabel: { fontSize: 10 } },
        { type: "category", data: dates, gridIndex: 1, axisLabel: { show: false } },
      ],
      yAxis: [
        { gridIndex: 0, scale: true, splitLine: { lineStyle: { opacity: 0.3 } } },
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
        },
        { name: "MA5", type: "line", data: mk("ma5", "#1971c2"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "MA20", type: "line", data: mk("ma20", "#f59f00"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "MA60", type: "line", data: mk("ma60", "#9c36b5"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "成交量", type: "bar", data: vols, xAxisIndex: 1, yAxisIndex: 1 },
      ],
    });
    const onResize = () => chartRef.current?.resize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [candles]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
