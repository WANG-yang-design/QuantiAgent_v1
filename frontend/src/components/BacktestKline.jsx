import { useEffect, useRef } from "react";
import * as echarts from "echarts";

/**
 * 回测买卖点K线图: 蜡烛图 + MA + 买卖点标记 + 策略包络带(盈亏着色)
 * props:
 *   candles  = [{date, open, high, low, close, volume, ma5, ma20, ma60}]
 *   marks    = [{date, price, side(BUY/SELL), qty, pnl}]
 *   roundTrips = [{buy_date, sell_date, cost, stop, sell_price, pnl, profit}]
 *               每笔完整建仓→平仓区间, 画包络带(盈利绿/亏损红) + 成本/止损线
 */
export default function BacktestKline({ candles, marks = [], roundTrips = [], height = 420 }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !candles?.length) return;
    if (!chartRef.current) chartRef.current = echarts.init(ref.current);

    const dates = candles.map((c) => c.date);
    const dateSet = new Set(dates);
    const kdata = candles.map((c) => [c.open, c.close, c.low, c.high]);
    const vols = candles.map((c) => ({ value: c.volume, itemStyle: { color: c.close >= c.open ? "#e03131" : "#2f9e44" } }));
    const mk = (key, color) => candles.map((c) => ({ value: c[key], itemStyle: { color } }));

    // 修复: 回测买卖点/包络带的日期可能落在K线范围外(回测区间与K线区间不一致),
    // echarts 在 category 轴上找不到该日期 → "Cannot read properties of undefined
    // (reading 'coord')" 白屏。先按K线日期过滤再画。
    const validMarks = marks.filter((m) => dateSet.has(m.date));
    const validTrips = roundTrips.filter(
      (rt) => dateSet.has(rt.buy_date) && (rt.sell_date == null || dateSet.has(rt.sell_date)));

    // 买卖点标记
    const markPoints = validMarks.map((m) => ({
      coord: [m.date, m.price],
      value: m.side === "BUY" ? "B" : "S",
      symbol: "pin",
      symbolSize: 30,
      itemStyle: { color: m.side === "BUY" ? "#e03131" : "#2f9e44" },
      label: { fontSize: 9, fontWeight: "bold", color: "#fff" },
    }));

    // 策略包络带: 每笔 round-trip 从买入日到卖出日的带状区间(盈利绿/亏损红) +
    //              成本线(蓝)与止损线(橙)随交易推进的阶梯连线
    // 修复: markArea 数据必须写成 [[起点, 终点], ...] 的"两元素数组"格式 ——
    // 原实现写成 {xAxis:[a,b], yAxis:[c,d]} 对象格式, echarts 5.5 的
    // markAreaTransform 会把对象当数组取 item[0](undefined) 返回空, 随后
    // markAreaFilter 对空项读取 item.coord 直接抛 "Cannot read properties of
    // undefined (reading 'coord')", 买卖点K线图白屏。
    const areas = [];
    const costLines = [];
    const stopLines = [];
    validTrips.forEach((rt) => {
      const endDate = rt.sell_date || dates[dates.length - 1];
      const color = rt.profit ? "rgba(47,158,68,0.18)" : "rgba(224,49,49,0.18)";
      areas.push([
        { xAxis: rt.buy_date, yAxis: rt.stop },
        { xAxis: endDate, yAxis: Math.max(rt.cost, rt.sell_price || rt.cost) },
      ].map((pt) => ({ ...pt, itemStyle: { color } })));
      costLines.push({ xAxis: rt.buy_date, yAxis: rt.cost });
      stopLines.push({ xAxis: rt.buy_date, yAxis: rt.stop });
      // 平仓点也延伸到卖出日(阶梯线终点)
      costLines.push({ xAxis: endDate, yAxis: rt.cost });
      stopLines.push({ xAxis: endDate, yAxis: rt.stop });
    });

    // 涨跌百分比基准: 区间首根K线收盘价(回测区间起点)
    const base = candles[0]?.close || 1;
    // 数据缩放窗口: 默认只显示后70%, 早期交易的标的下买卖点被挡在可视区外
    // (修复: "有些股票的买卖点没显示出来" —— 交易集中在区间前段的标的,
    //  标记确实画了, 但被 dataZoom 裁剪)。按买卖点首尾日期+5%留白自适应。
    let zoomStart = 0;
    let zoomEnd = 100;
    if (validMarks.length && dates.length) {
      const idxs = validMarks.map((m) => dates.indexOf(m.date)).filter((i) => i >= 0);
      if (idxs.length) {
        zoomStart = Math.max(0, Math.floor((Math.min(...idxs) / dates.length) * 100) - 5);
        zoomEnd = Math.min(100, Math.ceil(((Math.max(...idxs) + 1) / dates.length) * 100) + 5);
      }
    }
    const fmtVol = (v) => {
      if (v == null) return "-";
      const a = Math.abs(v);
      if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
      if (a >= 1e4) return (v / 1e4).toFixed(1) + "万";
      return String(Math.round(v));
    };

    chartRef.current.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          const c = candles[i];
          const chg = ((c.close / base - 1) * 100).toFixed(2);
          const dayMarks = validMarks.filter((m) => m.date === c.date);
          let extra = "";
          if (dayMarks.length) {
            extra = "<br/>" + dayMarks.map((m) =>
              `${m.side === "BUY" ? "🔼买入" : "🔽卖出"} ${m.qty}份 @ ${m.price}${m.pnl ? ` 盈亏${m.pnl >= 0 ? "+" : ""}${m.pnl}` : ""}`
            ).join("<br/>");
          }
          return `${c.date}<br/>开 ${c.open} 高 ${c.high}<br/>低 ${c.low} 收 ${c.close} (${chg}%)<br/>量 ${fmtVol(c.volume)}${extra}`;
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
          axisLabel: { fontSize: 9, formatter: (v) => `${((v / base - 1) * 100).toFixed(1)}%` },
        },
        {
          gridIndex: 1, splitLine: { show: false },
          axisLabel: { fontSize: 9, formatter: (v) => fmtVol(v) },
        },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: zoomStart, end: zoomEnd },
        { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 16, start: zoomStart, end: zoomEnd },
      ],
      series: [
        {
          name: "K线", type: "candlestick", data: kdata,
          itemStyle: { color: "#e03131", color0: "#2f9e44", borderColor: "#e03131", borderColor0: "#2f9e44" },
          markPoint: { data: markPoints, symbolOffset: [0, -10] },
          markArea: { data: areas, silent: true },
          markLine: {
            silent: true, symbol: "none",
            data: [
              ...costLines.map((p) => ({
                ...p,
                label: { formatter: "成本", fontSize: 9, position: "insideEndTop" },
                lineStyle: { color: "#1971c2", type: "dashed", width: 1, opacity: 0.85 },
              })),
              ...stopLines.map((p) => ({
                ...p,
                label: { formatter: "止损", fontSize: 9, position: "insideEndBottom" },
                lineStyle: { color: "#f59f00", type: "dashed", width: 1, opacity: 0.85 },
              })),
            ],
          },
        },
        { name: "MA5", type: "line", data: mk("ma5", "#1971c2"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "MA20", type: "line", data: mk("ma20", "#f59f00"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        { name: "MA60", type: "line", data: mk("ma60", "#9c36b5"), showSymbol: false, lineStyle: { width: 1 }, xAxisIndex: 0, yAxisIndex: 0 },
        // 隐藏绑定系列: 让右侧涨跌百分比轴被引用(修复: 不被 series 引用的轴不渲染)
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
      // 修复: ECharts 实例泄漏(SPA 切换时 dispose)
      if (chartRef.current) {
        chartRef.current.dispose();
        chartRef.current = null;
      }
    };
  }, [candles, marks, roundTrips]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
