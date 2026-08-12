// 临时实验：合并高相关组因子（13→12 之后的降维重组验证）
// 对比：C0 基线12因子 vs C1 等权合成 vs C2 现权重合成(sanity) vs C3 纯7参数 vs C4 IC加权
// 注意：本文件为实验主体，需与 predict-engine.js 拼接后运行（避免 vm 沙箱性能退化）：
//   python -c "open('_group_run.js','w',encoding='utf-8').write(open('predict-engine.js',encoding='utf-8').read()+'\n'+open('_factor_group.js',encoding='utf-8').read())"
const fs = require('fs');

const X = { computeParts, DEFAULT_WEIGHTS, DEFAULT_WEIGHTS_SELL, BUY_TH, SELL_TH };
const DATA = JSON.parse(fs.readFileSync('_kline_data.json', 'utf8'));
const STOCKS = DATA.stocks;

// ---------- 预计算每根K线 12 因子分（仅 score） ----------
console.log('precompute parts...');
const stockParts = STOCKS.map(st => {
  const rows = st.rows;
  const scores = [];
  for (let t = 0; t < rows.length; t++) {
    const parts = X.computeParts(rows, t, {});
    const m = {};
    parts.forEach(p => { m[p.key] = p.score; });
    scores.push(m);
  }
  return { symbol: st.symbol, rows, scores };
});
console.log('done. stocks=', stockParts.length);

// ---------- 全样本因子 IC（因子分 × 后5日收益 pearson） ----------
function computeIC() {
  const acc = {};
  const keys = ['main', 'vol', 'turnover', 'volratio', 'rsi', 'kdj', 'bias', 'macd', 'candle', 'ma', 'obv', 'mom5'];
  keys.forEach(k => acc[k] = { n: 0, sx: 0, sy: 0, sxy: 0, sx2: 0, sy2: 0 });
  stockParts.forEach(sp => {
    const rows = sp.rows;
    for (let t = 40; t + 5 < rows.length; t++) {
      const c0 = rows[t].close, c5 = rows[t + 5].close;
      if (!c0 || !c5) continue;
      const y = c5 / c0 - 1;
      keys.forEach(k => {
        const x = sp.scores[t][k];
        if (x == null) return;
        const a = acc[k];
        a.n++; a.sx += x; a.sy += y; a.sxy += x * y; a.sx2 += x * x; a.sy2 += y * y;
      });
    }
  });
  const ic = {};
  keys.forEach(k => {
    const a = acc[k];
    if (a.n < 1000) { ic[k] = null; return; }
    const cov = (a.sxy - a.sx * a.sy / a.n) / a.n;
    const vx = a.sx2 / a.n - (a.sx / a.n) ** 2;
    const vy = a.sy2 / a.n - (a.sy / a.n) ** 2;
    ic[k] = (vx > 0 && vy > 0) ? cov / Math.sqrt(vx * vy) : 0;
  });
  return ic;
}
const IC = computeIC();
console.log('IC(千分位):', Object.fromEntries(Object.entries(IC).map(([k, v]) => [k, Math.round(v * 1000)])));

// ---------- 评分器 ----------
function weightedSMap(scoreMap, weights) {
  let tw = 0, s = 0;
  for (const k in weights) { const w = weights[k]; if (w) { tw += w; s += w * (scoreMap[k] || 0); } }
  return tw ? clamp(s / tw, -100, 100) : 0;
}
// 组因子：groups = { gKey: [subKeys] }, synth(vals)=>组评分
function groupScoreMap(scoreMap, groups, synth) {
  const m = {};
  for (const g in groups) m[g] = synth(groups[g].map(k => scoreMap[k] || 0));
  return m;
}

// ---------- 信号生成（与 computeSignalsDual 同逻辑，基于预计算分） ----------
function genSignals(scores, wBuy, wSell, groups, synth, buyTh, sellTh) {
  const signals = [];
  let prevBuy = null, prevSell = null;
  for (let t = 40; t < scores.length; t++) {
    const sm = groups ? groupScoreMap(scores[t], groups, synth) : scores[t];
    const Sbuy = weightedSMap(sm, wBuy);
    const Ssell = weightedSMap(sm, wSell);
    if (prevBuy != null && Sbuy >= buyTh && prevBuy < buyTh - 8) signals.push({ t, type: 'buy', S: Sbuy });
    if (prevSell != null && Ssell <= sellTh && prevSell > sellTh + 8) signals.push({ t, type: 'sell', S: Ssell });
    prevBuy = Sbuy; prevSell = Ssell;
  }
  return signals;
}

// ---------- 统计（主口径后5日 + 样本外2023后 + 简化收益） ----------
function statsAll(rows, signals, fromDate) {
  let buyN = 0, buyWin = 0, sellN = 0, sellWin = 0;
  let buySum = 0, sellSum = 0, oosBuyN = 0, oosBuyWin = 0, oosSellN = 0, oosSellWin = 0;
  signals.forEach(sg => {
    const t = sg.t;
    if (t + 5 >= rows.length) return;
    const c0 = rows[t].close, c5 = rows[t + 5].close;
    if (!c0 || !c5) return;
    const ret = c5 / c0 - 1;
    const oos = fromDate && rows[t].date >= fromDate;
    if (sg.type === 'buy') {
      buyN++; if (ret > 0) buyWin++; buySum += ret;
      if (oos) { oosBuyN++; if (ret > 0) oosBuyWin++; }
    } else {
      sellN++; if (ret < 0) sellWin++; sellSum += ret;
      if (oos) { oosSellN++; if (ret < 0) oosSellWin++; }
    }
  });
  return {
    buy: { n: buyN, hit: buyN ? buyWin / buyN * 100 : null, avg: buyN ? buySum / buyN * 100 : null },
    sell: { n: sellN, hit: sellN ? sellWin / sellN * 100 : null, avg: sellN ? sellSum / sellN * 100 : null },
    oosBuy: { n: oosBuyN, hit: oosBuyN ? oosBuyWin / oosBuyN * 100 : null },
    oosSell: { n: oosSellN, hit: oosSellN ? oosSellWin / oosSellN * 100 : null },
  };
}
function mergeStats(list) {
  const out = {};
  ['buy', 'sell', 'oosBuy', 'oosSell'].forEach(k => {
    let n = 0, hit = 0;
    list.forEach(s => { n += s[k].n; hit += (s[k].hit || 0) * s[k].n; });
    out[k] = { n, hit: n ? hit / n : null };
  });
  let bs = 0;
  list.forEach(s => { bs += (s.buy.avg || 0) * s.buy.n; });
  out.buy.avg = out.buy.n ? bs / out.buy.n : null;
  let ss = 0;
  list.forEach(s => { ss += (s.sell.avg || 0) * s.sell.n; });
  out.sell.avg = out.sell.n ? ss / out.sell.n : null;
  return out;
}
// 随机入场基线（按样本数加权）
function baselineAll() {
  let n = 0, up = 0, sum = 0;
  stockParts.forEach(sp => {
    const rows = sp.rows;
    for (let t = 40; t + 5 < rows.length; t++) {
      const c0 = rows[t].close, c5 = rows[t + 5].close;
      if (!c0 || !c5) continue;
      const r = c5 / c0 - 1; n++; if (r > 0) up++; sum += r;
    }
  });
  return { hit: up / n * 100, avg: sum / n * 100, n };
}

// ---------- 配置 ----------
const WB = X.DEFAULT_WEIGHTS, WS = X.DEFAULT_WEIGHTS_SELL;
const groups = { oversold: ['rsi', 'kdj', 'bias', 'mom5'], volgroup: ['vol', 'turnover', 'volratio'] };
const keys = ['main', 'vol', 'turnover', 'volratio', 'rsi', 'kdj', 'bias', 'macd', 'candle', 'ma', 'obv', 'mom5'];
// 7 组权重（main/oversold/volgroup/macd/candle/ma/obv）
const wbG = { main: 10, oversold: 12 + 20 + 22 + 22, volgroup: 12 + 10 + 10, macd: 8, candle: 10, ma: 6, obv: 10 };
const wsG = { main: 16, oversold: 14 + 12 + 12 + 24, volgroup: 12 + 12 + 10, macd: 10, candle: 10, ma: 8, obv: 8 };
const wbG10 = { main: 10, oversold: 10, volgroup: 10, macd: 10, candle: 10, ma: 10, obv: 10 };
const wsG10 = { main: 10, oversold: 10, volgroup: 10, macd: 10, candle: 10, ma: 10, obv: 10 };
// IC 加权合成（带符号，组内 IC 归一）
function makeIcSynth(subKeys) {
  return vals => {
    let num = 0, den = 0;
    subKeys.forEach((k, i) => { const ic = IC[k] || 0; num += ic * vals[i]; den += Math.abs(ic); });
    return den ? num / den : 0;
  };
}

const CFGS = [
  { name: 'C0 基线12因子(现权重)', groups: null, synth: null, wb: WB, ws: WS },
  { name: 'C1 等权合成+组权重原和', groups, synth: vals => vals.reduce((a, b) => a + b, 0) / vals.length, wb: wbG, ws: wsG },
  { name: 'C2 现权重合成(sanity)', groups, synth: null, wb: wbG, ws: wsG },
  { name: 'C3 等权合成+纯7参数', groups, synth: vals => vals.reduce((a, b) => a + b, 0) / vals.length, wb: wbG10, ws: wsG10 },
  { name: 'C4 IC加权合成+组权重原和', groups, synth: null, wb: wbG, ws: wsG },
];
// C2 用现权重归一合成
CFGS[2].synth = vals => { const w = [12, 20, 22, 22]; let s = 0, tw = 0; vals.forEach((x, i) => { s += w[i] * x; tw += w[i]; }); return tw ? s / tw : 0; };
// 但 C2 volgroup 的现权重是 [12,10,10]
// 修正：C2 需要组内权重表
const C2_synth = (subKeys, ws2) => vals => { let s = 0, tw = 0; subKeys.forEach((k, i) => { const w = ws2[i]; s += w * vals[i]; tw += w; }); return tw ? s / tw : 0; };
CFGS[2].synth = C2_synth(['rsi', 'kdj', 'bias', 'mom5'], [12, 20, 22, 22]);
// 但 volgroup 也要现权重——synth 是单函数，组不同权重无法表达。C2 改为两组分开 synth：
function synthByGroup(groupsDef, synthMap) {
  return (scoreMap, groups) => { const m = {}; for (const g in groups) m[g] = synthMap[g](groups[g].map(k => scoreMap[k] || 0)); return m; };
}
// 重写 C2 配置实现
CFGS[2].groupSynth = true;
const c2SynthMap = {
  oversold: C2_synth(['rsi', 'kdj', 'bias', 'mom5'], [12, 20, 22, 22]),
  volgroup: C2_synth(['vol', 'turnover', 'volratio'], [12, 10, 10]),
};
// C4 用 IC 加权
CFGS[4].groupSynth = true;
const c4SynthMap = {
  oversold: makeIcSynth(['rsi', 'kdj', 'bias', 'mom5']),
  volgroup: makeIcSynth(['vol', 'turnover', 'volratio']),
};

const FROM = '2023-01-01';
const BASE = baselineAll();
console.log('\n随机入场基线: hit=%.1f%% avg=%.2f%% n=%d\n', BASE.hit, BASE.avg, BASE.n);

CFGS.forEach(cfg => {
  const partsList = [];
  stockParts.forEach(sp => {
    const { rows, scores } = sp;
    let signals;
    if (!cfg.groups) {
      signals = genSignals(scores, cfg.wb, cfg.ws, null, null, X.BUY_TH, X.SELL_TH);
    } else if (cfg.groupSynth) {
      // 每只股票两组分别合成
      const synthMap = cfg === CFGS[2] ? c2SynthMap : c4SynthMap;
      const groupScores = [];
      for (let t = 0; t < scores.length; t++) {
        const m = {};
        for (const g in cfg.groups) m[g] = synthMap[g](cfg.groups[g].map(k => scores[t][k] || 0));
        // 独立因子直接透传
        keys.filter(k => !['rsi', 'kdj', 'bias', 'mom5', 'vol', 'turnover', 'volratio'].includes(k)).forEach(k => { m[k] = scores[t][k]; });
        groupScores.push(m);
      }
      signals = genSignals(groupScores, cfg.wb, cfg.ws, null, null, X.BUY_TH, X.SELL_TH);
    } else {
      signals = genSignals(scores, cfg.wb, cfg.ws, cfg.groups, cfg.synth, X.BUY_TH, X.SELL_TH);
    }
    partsList.push(statsAll(rows, signals, FROM));
  });
  const m = mergeStats(partsList);
  console.log(`== ${cfg.name}`);
  console.log(`  buy: n=${m.buy.n} hit=${m.buy.hit ? m.buy.hit.toFixed(1) : '-'}% (超额 ${m.buy.hit ? (m.buy.hit - BASE.hit).toFixed(1) : '-'}pt) avg=${m.buy.avg ? m.buy.avg.toFixed(2) : '-'}%`);
  console.log(`  sell: n=${m.sell.n} hit=${m.sell.hit ? m.sell.hit.toFixed(1) : '-'}% avg=${m.sell.avg ? m.sell.avg.toFixed(2) : '-'}%`);
  console.log(`  oos(2023后): buy n=${m.oosBuy.n} hit=${m.oosBuy.hit ? m.oosBuy.hit.toFixed(1) : '-'}% | sell n=${m.oosSell.n} hit=${m.oosSell.hit ? m.oosSell.hit.toFixed(1) : '-'}%`);
});

// ===== 补充：C3（等权合成+纯7参数）扫阈值，验证降参是否可能恢复命中率 =====
{
  console.log('\n== C3 等权合成+纯7参数 阈值扫描（buyTh/sellTh 对称）==');
  const wbE = { main: 10, oversold: 10, volgroup: 10, macd: 10, candle: 10, ma: 10, obv: 10 };
  const wsE = { main: 10, oversold: 10, volgroup: 10, macd: 10, candle: 10, ma: 10, obv: 10 };
  const synthEq = vals => vals.reduce((a, b) => a + b, 0) / vals.length;
  [35, 30, 25, 20, 15, 12, 10, 8].forEach(th => {
    const partsList = [];
    stockParts.forEach(sp => {
      const { rows, scores } = sp;
      const signals = genSignals(scores, wbE, wsE, groups, synthEq, th, -th);
      partsList.push(statsAll(rows, signals, FROM));
    });
    const m = mergeStats(partsList);
    console.log(`  th=${th}: buy n=${m.buy.n} hit=${m.buy.hit ? m.buy.hit.toFixed(1) : '-'}% (超额 ${m.buy.hit ? (m.buy.hit - BASE.hit).toFixed(1) : '-'}pt) avg=${m.buy.avg ? m.buy.avg.toFixed(2) : '-'}% | sell n=${m.sell.n} hit=${m.sell.hit ? m.sell.hit.toFixed(1) : '-'}% | oos buy=${m.oosBuy.hit ? m.oosBuy.hit.toFixed(1) : '-'}%`);
  });
}
