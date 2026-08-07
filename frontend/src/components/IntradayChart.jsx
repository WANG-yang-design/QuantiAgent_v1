import { useEffect, useRef } from "react";
import * as echarts from "echarts";

/**
 * 多日分时图: 价格线 + 均价线 + 成交量
 * props: days = [{date:"2026-08-04", prev_close, points:[{time,price,avg,volume}]}]
 * 特性:
 *  - 天与天之间留空(日期头 + 分隔线), 一眼区分不同交易日
 *  - Y轴上下留白(不以最低价贴0轴), 含昨收基准虚线
 */
export default function IntradayChart({ days = [], height = 420 }) {
  const ref = useRef(null);
  const chartRef = useRef(null);

  useEffect(() => {
    if (!ref.current || !days?.length) return;
    if (!chartRef.current) chartRef.current = echarts.init(ref.current);

    // 展平: 每天前插一个"日期头"(无数据), 天与天之间再插一个空位
    const cats = [];
    const priceData = [];
    const avgData = [];
    const volData = [];
    const headIdx = [];       // 日期头所在索引(画分隔线/日期标签)
    let allPrices = [];
    days.forEach((d, di) => {
      if (di > 0) { cats.push(""); priceData.push(null); avgData.push(null); volData.push(null); }
      headIdx.push(cats.length);
      cats.push(`HEAD_${di}`);
      priceData.push(null); avgData.push(null); volData.push(null);
      d.points.forEach((p) => {
        cats.push(p.time);
        priceData.push(p.price);
        avgData.push(p.avg);
        volData.push(p.volume);
        allPrices.push(p.price);
      });
    });

    // Y轴: 全部价格 + 昨收, 上下各留 8% 缓冲(不以最低价贴轴)
    let lo = Math.min(...allPrices);
    let hi = Math.max(...allPrices);
    days.forEach((d) => {
      if (d.prev_close > 0) { lo = Math.min(lo, d.prev_close); hi = Math.max(hi, d.prev_close); }
    });
    const pad = (hi - lo) * 0.12 || hi * 0.01 || 0.01;
    const yMin = Math.floor((lo - pad) * 1000) / 1000;
    const yMax = Math.ceil((hi + pad) * 1000) / 1000;

    const lastDay = days[days.length - 1];
    const lastPrice = lastDay.points[lastDay.points.length - 1]?.price ?? 0;
    const prevClose = lastDay.prev_close;
    const up = lastPrice >= prevClose;
    const priceColor = up ? "#e03131" : "#2f9e44";
    // 成交量单位: 万/亿(修复: 原为原始股数, 数字巨大且单位不明)
    const fmtVol = (v) => {
      if (v == null) return "-";
      const a = Math.abs(v);
      if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
      if (a >= 1e4) return (v / 1e4).toFixed(1) + "万";
      return String(Math.round(v));
    };
    const vols = volData.map((v, i) => {
      // 成交量颜色: 与所在交易日的昨收比较
      let dayPrev = days[days.length - 1].prev_close;
      for (const d of days) {
        if (d.points[0] && cats[i] && cats[i] >= d.points[0].time) dayPrev = d.prev_close;
      }
      return {
        value: v,
        itemStyle: { color: priceData[i] != null && priceData[i] >= dayPrev ? "#e03131" : "#2f9e44" },
      };
    });

    const mkLineData = headIdx.map((idx) => ({ xAxis: idx }));
    const labelFormatter = (idx) => {
      const head = headIdx.findIndex((h) => h === idx);
      if (head >= 0 && days[head]) {
        const d = days[head].date;
        return `${d.slice(5)} (${days[head].points.length}点)`;
      }
      return cats[idx] || "";
    };

    chartRef.current.setOption({
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          // 找到所在交易日
          let day = null, dayIdx = -1;
          for (let k = 0; k < days.length; k++) {
            if (headIdx[k] <= i) { day = days[k]; dayIdx = k; }
          }
          if (!day || priceData[i] == null) return "";
          const prev = day.prev_close;
          const chg = prev ? ((priceData[i] / prev - 1) * 100).toFixed(2) : "-";
          return `${day.date} ${cats[i]}<br/>价格 ${priceData[i]} (${chg}%)<br/>均价 ${avgData[i]}<br/>量 ${volData[i]}`;
        },
      },
      legend: { data: ["价格", "均价"], top: 0, textStyle: { fontSize: 11 } },
      grid: [
        { left: 60, right: 70, top: 28, height: "55%" },
        { left: 60, right: 70, top: "72%", height: "18%" },
      ],
      xAxis: [
        {
          type: "category", data: cats, gridIndex: 0,
          axisLabel: { fontSize: 9, interval: (idx) => idx % 30 === 0 || headIdx.includes(idx), formatter: labelFormatter },
        },
        { type: "category", data: cats, gridIndex: 1, axisLabel: { show: false } },
      ],
      yAxis: [
        {
          gridIndex: 0, min: yMin, max: yMax,
          splitLine: { lineStyle: { opacity: 0.3 } },
        },
        {
          gridIndex: 0, min: yMin, max: yMax, position: "right", splitLine: { show: false },
          // 右侧涨跌百分比轴(以最近一个交易日的昨收为基准, 修复: 原只有价格轴)
          axisLabel: { fontSize: 9, formatter: (v) => `${((v / (prevClose || yMin || 1) - 1) * 100).toFixed(1)}%` },
        },
        {
          gridIndex: 1, splitLine: { show: false },
          axisLabel: { fontSize: 9, formatter: (v) => fmtVol(v) },
        },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 0, end: 100 },
      ],
      series: [
        {
          name: "价格", type: "line", data: priceData,
          showSymbol: false, connectNulls: false,
          lineStyle: { width: 1.2, color: priceColor },
          itemStyle: { color: priceColor },
          markLine: prevClose > 0 ? {
            symbol: "none", silent: true,
            lineStyle: { color: "#868e96", type: "dashed", width: 1 },
            label: { show: true, position: "insideEndTop", fontSize: 10, formatter: `最新${lastPrice} 昨收${prevClose}` },
            data: [{ yAxis: prevClose }],
          } : undefined,
        },
        {
          name: "均价", type: "line", data: avgData,
          showSymbol: false, connectNulls: false,
          lineStyle: { width: 1, color: "#f59f00", type: "dashed" },
        },
        {
          name: "日期分隔", type: "line", data: [],
          markLine: {
            symbol: "none", silent: true,
            lineStyle: { color: "#adb5bd", width: 1 },
            label: { show: false },
            data: mkLineData,
          },
        },
        // 隐藏绑定系列: 让右侧涨跌百分比轴被引用(修复: 不被 series 引用的轴不渲染)
        { name: "涨跌%", type: "line", data: priceData, xAxisIndex: 0, yAxisIndex: 1,
          showSymbol: false, silent: true, legendHoverLink: false,
          lineStyle: { opacity: 0 }, itemStyle: { opacity: 0 }, tooltip: { show: false } },
        { name: "成交量", type: "bar", data: vols, xAxisIndex: 1, yAxisIndex: 2 },
      ],
    });
    const onResize = () => chartRef.current?.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (chartRef.current) {
        chartRef.current.dispose();
        chartRef.current = null;
      }
    };
  }, [days]);

  return <div ref={ref} style={{ height, width: "100%" }} />;
}
