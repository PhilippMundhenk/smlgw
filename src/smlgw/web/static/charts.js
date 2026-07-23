// Minimal self-contained canvas charts (no external dependencies) for the
// dashboard: time-series line charts and radial gauges. Stat panels are plain
// DOM and handled in the page. Everything is theme-aware via CSS variables.

(function (global) {
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  const PALETTE = ['#4f8cff', '#35c46a', '#e0a53b', '#e5484d', '#a56bff', '#26c6da', '#ff7ab6', '#9aa3b2'];

  function setupHiDPI(canvas) {
    const ratio = global.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    canvas.width = w * ratio;
    canvas.height = h * ratio;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { ctx, w, h };
  }

  function niceTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    const range = max - min;
    const raw = range / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    let step;
    if (norm < 1.5) step = 1; else if (norm < 3) step = 2; else if (norm < 7) step = 5; else step = 10;
    step *= mag;
    const start = Math.ceil(min / step) * step;
    const ticks = [];
    for (let v = start; v <= max + step * 0.001; v += step) ticks.push(v);
    return ticks;
  }

  function fmtNum(v) {
    if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
    if (Math.abs(v) >= 1) return (Math.round(v * 100) / 100).toString();
    return (Math.round(v * 10000) / 10000).toString();
  }

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  // series: [{ label, color, points: [[ts, value], ...] }]
  function lineChart(canvas, series, options) {
    options = options || {};
    const { ctx, w, h } = setupHiDPI(canvas);
    ctx.clearRect(0, 0, w, h);
    const text = cssVar('--muted', '#9aa3b2');
    const grid = cssVar('--border', '#2b303b');
    const padL = 46, padR = 10, padT = 8, padB = 20;
    const plotW = w - padL - padR, plotH = h - padT - padB;

    let tMin = Infinity, tMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    let any = false;
    series.forEach((s) => s.points.forEach((p) => {
      any = true;
      if (p[0] < tMin) tMin = p[0]; if (p[0] > tMax) tMax = p[0];
      if (p[1] < yMin) yMin = p[1]; if (p[1] > yMax) yMax = p[1];
    }));
    if (!any) {
      ctx.fillStyle = text; ctx.font = '12px system-ui'; ctx.textAlign = 'center';
      ctx.fillText('no data yet', w / 2, h / 2);
      return null;
    }
    if (options.tMin != null) tMin = options.tMin;
    if (options.tMax != null) tMax = options.tMax;
    if (yMin === yMax) { yMin -= 1; yMax += 1; }
    const yPad = (yMax - yMin) * 0.1; yMin -= yPad; yMax += yPad;
    if (options.yMin != null) yMin = options.yMin;

    const xOf = (t) => padL + ((t - tMin) / (tMax - tMin || 1)) * plotW;
    const yOf = (v) => padT + plotH - ((v - yMin) / (yMax - yMin || 1)) * plotH;

    // y grid + labels
    ctx.strokeStyle = grid; ctx.fillStyle = text; ctx.font = '10px system-ui';
    ctx.lineWidth = 1; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    niceTicks(yMin, yMax, 4).forEach((v) => {
      const y = yOf(v);
      if (y < padT - 1 || y > padT + plotH + 1) return;
      ctx.globalAlpha = 0.5; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.globalAlpha = 1; ctx.fillText(fmtNum(v), padL - 6, y);
    });
    // x labels (start / mid / end)
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    [tMin, (tMin + tMax) / 2, tMax].forEach((t) => ctx.fillText(fmtTime(t), xOf(t), padT + plotH + 5));

    // lines
    series.forEach((s, i) => {
      const color = s.color || PALETTE[i % PALETTE.length];
      ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.beginPath();
      s.points.forEach((p, j) => {
        const x = xOf(p[0]), y = yOf(p[1]);
        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    });

    return { xOf, yOf, tMin, tMax, yMin, yMax, padL, padT, plotW, plotH };
  }

  // Radial gauge for a single value.
  function gauge(canvas, value, min, max, options) {
    options = options || {};
    const { ctx, w, h } = setupHiDPI(canvas);
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h * 0.72, r = Math.min(w / 2, h * 0.72) - 8;
    const start = Math.PI, end = 2 * Math.PI;
    const frac = Math.max(0, Math.min(1, (value - min) / ((max - min) || 1)));
    ctx.lineWidth = Math.max(8, r * 0.22); ctx.lineCap = 'round';
    ctx.strokeStyle = cssVar('--panel-2', '#232733');
    ctx.beginPath(); ctx.arc(cx, cy, r, start, end); ctx.stroke();
    ctx.strokeStyle = options.color || cssVar('--accent', '#4f8cff');
    ctx.beginPath(); ctx.arc(cx, cy, r, start, start + (end - start) * frac); ctx.stroke();
    ctx.fillStyle = cssVar('--text', '#e6e8ee'); ctx.textAlign = 'center';
    ctx.font = '600 22px system-ui';
    ctx.fillText(fmtNum(value) + (options.unit ? ' ' + options.unit : ''), cx, cy - 2);
    ctx.fillStyle = cssVar('--muted', '#9aa3b2'); ctx.font = '10px system-ui';
    ctx.fillText(fmtNum(min), cx - r, cy + 12);
    ctx.fillText(fmtNum(max), cx + r, cy + 12);
  }

  global.SmlCharts = { lineChart, gauge, fmtNum, fmtTime, PALETTE };
})(window);
