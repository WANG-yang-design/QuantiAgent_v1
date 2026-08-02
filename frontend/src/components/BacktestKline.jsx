import { useEffect, useRef } from "react";
import * as echarts from "echarts";

/**
 * 回测买卖点K线图: 蜡烛图 + MA + 买卖点标记 + 成本/止损包络虚线
 * props:
 *   candles  = [{date, open, high, low, close, volume, ma5, ma20, ma60}]
 *   marks    = [{date, price, side(BUY/SELL), qty, pnl}]
 *   envelopes= [{date, cost, stop}]  策略包络(成本线+止损线)
 */
export default function BacktestKline({ candles, marks = [], envelopes = [], height = 420 }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !candles?.length) return;
    if (!chartRef.current) chartRef.current = echarts.init(ref.current);

    const dates = candles.map((c) => c.date);
    const kdata = candles.map((c) => [c.open, c.close, c.low, c.high]);
    const vols = candles.map((c) => ({ value: c.volume, itemStyle: { color: c.close >= c.open ? "#e03131" : "#2f9e44" } }));
    const mk = (key, color) => candles.map((c) => ({ value: c[key], itemStyle: { color } }));

    // 买卖点标记
    const markPoints = marks.map((m) => ({
      coord: [m.date, m.price],
      value: m.side === "BUY" ? "B" : "S",
      symbol: "pin",
      symbolSize: 32,
      itemStyle: { color: m.side === "BUY" ? "#e03131" : "#2f9e44" },
      label: { fontSize: 10, fontWeight: "bold", color: "#fff" },
    }));

    // 策略包络: 每笔买入的成本线(红虚) + 止损线(绿虚), 持续到下一笔买入
    const lines = [];
    envelopes.forEach((e) => {
      lines.push({
        xAxis: e.date, yAxis: e.cost,
        label: { formatter: "成本 " + e.cost.toFixed(3), fontSize: 9 },
        lineStyle: { color: "#1971c2", type: "dashed", width: 1 },
      });
      lines.push({
        xAxis: e.date, yAxis: e.stop,
        label: { formatter: "止损 " + e.stop.toFixed(3), fontSize: 9 },
        lineStyle: { color: "#f59f00", type: "dashed", width: 1 },
      });
    });

    chartRef.current.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          const c = candles[i];
          const dayMarks = marks.filter((m) => m.date === c.date);
          let extra = "";
          if (dayMarks.length) {
            extra = "<br/>" + dayMarks.map((m) =>
              `${m.side === "BUY" ? "🔼买入" : "🔽卖出"} ${m.qty}份 @ ${m.price}${m.pnl ? ` 盈亏${m.pnl >= 0 ? "+" : ""}${m.pnl}` : ""}`
            ).join("<br/>");
          }
          return `${c.date}<br/>开 ${c.open} 高 ${c.high}<br/>低 ${c.low} 收 ${c.close}${extra}`;
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
        { type: "inside", xAxisIndex: [0, 1], start: 30, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 16 },
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: kdata,
          itemStyle: { color: "#e03131", color0: "#2f9e44", borderColor: "#e03131", borderColor0: "#2f9e44" },
          markPoint: { data: markPoints, symbolOffset: [0, -12] },
          markLine: { data: lines, silent: true, symbol: "none" },
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
  }, [candles, marks, envelopes]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
