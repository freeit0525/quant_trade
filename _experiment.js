// 临时实验脚本：信号质量扫描（后五日收益率为权威口径）
// 加载 predict-engine.js + 导出的数据，扫描 BUY_TH/SELL_TH 门槛、强度分档、时间切分、市场过滤
// 用法: node _experiment.js
const fs = require('fs');
const vm = require('vm');

// 1. 加载 predict-engine.js（浏览器全局脚本，用 vm 注入全局）
const engineSrc = fs.readFileSync('predict-engine.js', 'utf-8');
const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(engineSrc, sandbox);
const {
  computeParts, weightedS, factorFundamental, DEFAULT_WEIGHTS, DEFAULT_WEIGHTS_SELL,
} = sandbox;

const data = JSON.parse(fs.readFileSync('_kline_data.json', 'utf-8'));
const stocks = data.stocks;

function num(x) { return (x === null || x === undefined || x === '' || isNaN(x)) ? null : Number(x); }

// 构建 info
function infoOf(s) {
  return {
    symbol: s.symbol, name: s.name, list_date: s.list_date,
    float_market_cap: s.float_market_cap, total_market_cap: s.total_market_cap, industry: ''
  };
}

// 每只股票预计算 __obv（引擎 factorOBV 复用）
function prepareRows(rows) {
  const obv = [0];
  let val = 0;
  for (let j = 1; j < rows.length; j++) {
    const c = num(rows[j].close), pc = num(rows[j - 1].close);
    const vol = num(rows[j].volume) || 0;
    if (c != null && pc != null && vol > 0) {
      if (c > pc) val += vol; else if (c < pc) val -= vol;
    }
    obv.push(val);
  }
  rows.__obv = obv;
  return rows;
}

// 全部股票预处理
stocks.forEach(s => prepareRows(s.rows));

// ============ 通用回测 ============
// opts: { buyTh, sellTh, minS, onlyOversold, onlyOverheat, fromDate, toDate, evalDays }
function runBacktest(opts = {}) {
  const {
    buyTh = 25, sellTh = -25, minS = 0, onlyOversold = false, onlyOverheat = false,
    fromDate = null, toDate = null, evalDays = 5,
  } = opts;
  const results = [];
  stocks.forEach(s => {
    const rows = s.rows;
    const info = infoOf(s);
    let prevBuy = null, prevSell = null;
    for (let t = 40; t < rows.length; t++) {
      if (fromDate && rows[t].date < fromDate) continue;
      if (toDate && rows[t].date > toDate) continue;
      const parts = computeParts(rows, t, info);
      const Sbuy = weightedS(parts, DEFAULT_WEIGHTS);
      const Ssell = weightedS(parts, DEFAULT_WEIGHTS_SELL);
      // 市场过滤：近5日跌幅/涨幅
      if (onlyOversold) {
        const c0 = num(rows[t].close), c5 = t >= 5 ? num(rows[t - 5].close) : null;
        if (c0 == null || c5 == null || c0 <= 0 || c5 <= 0 || c0 / c5 - 1 > -0.08) { prevBuy = Sbuy; prevSell = Ssell; continue; }
      }
      if (onlyOverheat) {
        const c0 = num(rows[t].close), c5 = t >= 5 ? num(rows[t - 5].close) : null;
        if (c0 == null || c5 == null || c0 <= 0 || c5 <= 0 || c0 / c5 - 1 < 0.12) { prevBuy = Sbuy; prevSell = Ssell; continue; }
      }
      if (prevBuy != null && Sbuy >= buyTh && Sbuy >= minS && prevBuy < buyTh - 8) {
        results.push({ type: 'buy', t, date: rows[t].date, S: Sbuy, close0: num(rows[t].close), symbol: s.symbol });
      }
      if (prevSell != null && Ssell <= sellTh && Ssell <= -minS && prevSell > sellTh + 8) {
        results.push({ type: 'sell', t, date: rows[t].date, S: Ssell, close0: num(rows[t].close), symbol: s.symbol });
      }
      prevBuy = Sbuy;
      prevSell = Ssell;
    }
  });
  // 计算 N 日收益
  let buyCnt = 0, buyWin = 0, buyRetSum = 0, buyRetSq = 0;
  let sellCnt = 0, sellWin = 0, sellRetSum = 0;
  const buyRets = [], sellRets = [];
  results.forEach(r => {
    const s = stocks.find(x => x.symbol === r.symbol);
    const rows = s.rows;
    if (r.t + evalDays >= rows.length) return;
    const closeN = num(rows[r.t + evalDays].close);
    if (r.close0 == null || r.close0 <= 0 || closeN == null) return;
    const ret = closeN / r.close0 - 1;
    if (r.type === 'buy') {
      buyCnt++; if (ret > 0) buyWin++;
      buyRetSum += ret; buyRets.push(ret);
    } else {
      sellCnt++; if (ret < 0) sellWin++;
      sellRetSum += ret; sellRets.push(ret);
    }
  });
  const avg = a => a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
  const std = a => a.length > 1 ? Math.sqrt(a.reduce((s, x) => s + (x - avg(a)) ** 2, 0) / (a.length - 1)) : null;
  return {
    buyCnt, buyHit: buyCnt ? buyWin / buyCnt * 100 : null, buyAvgRet: avg(buyRets),
    buyStd: std(buyRets), buyPos: buyCnt ? (buyRets.filter(x => x > 0).length) / buyCnt * 100 : null,
    sellCnt, sellHit: sellCnt ? sellWin / sellCnt * 100 : null, sellAvgRet: avg(sellRets),
    sellStd: std(sellRets), sellPos: sellCnt ? (sellRets.filter(x => x < 0).length) / sellCnt * 100 : null,
  };
}

// ============ 随机基线（全样本任意一天后 N 日收益） ============
function baseline(evalDays = 5, fromDate = null, toDate = null) {
  let cnt = 0, up = 0, sum = 0;
  const rets = [];
  stocks.forEach(s => {
    const rows = s.rows;
    for (let t = 40; t + evalDays < rows.length; t++) {
      const d = rows[t].date;
      if (fromDate && d < fromDate) continue;
      if (toDate && d > toDate) continue;
      const c0 = num(rows[t].close), cn = num(rows[t + evalDays].close);
      if (c0 == null || c0 <= 0 || cn == null) continue;
      const r = cn / c0 - 1;
      cnt++; if (r > 0) up++; sum += r; rets.push(r);
    }
  });
  return { cnt, upRate: cnt ? up / cnt * 100 : null, avgRet: cnt ? sum / cnt : null };
}

// ============ 打印 ============
function fmt(o) {
  const p = (v) => v == null ? '-' : v.toFixed(2);
  return `买${o.buyCnt}次 命中${p(o.buyHit)}% 均收${p(o.buyAvgRet)}% [std ${p(o.buyStd)}] | 卖${o.sellCnt}次 命中${p(o.sellHit)}% 均收${p(o.sellAvgRet)}%`;
}

console.log('=== 股票池 ===', stocks.length, '只');
const b = baseline();
console.log('随机基线(全样本): 样本', b.cnt, '上涨', b.upRate.toFixed(2) + '%', '均收益', b.avgRet.toFixed(3) * 100 + '%');

console.log('\n=== 1. BUY_TH 门槛扫描（其余默认） ===');
for (const th of [25, 30, 35, 40, 45, 50]) {
  const o = runBacktest({ buyTh: th, sellTh: -25 });
  console.log(`BUY_TH=${th}: ${fmt(o)}`);
}

console.log('\n=== 2. SELL_TH 门槛扫描 ===');
for (const th of [-25, -30, -35, -40]) {
  const o = runBacktest({ buyTh: 25, sellTh: th });
  console.log(`SELL_TH=${th}: ${fmt(o)}`);
}

console.log('\n=== 3. 信号强度分档（S>=minS 才记信号） ===');
for (const ms of [0, 30, 35, 40, 50]) {
  const o = runBacktest({ buyTh: 25, sellTh: -25, minS: ms });
  console.log(`minS=±${ms}: ${fmt(o)}`);
}

console.log('\n=== 4. 超跌/超涨过滤（近5日涨跌） ===');
for (const tag of ['oversold', 'overheat']) {
  const o = runBacktest({ buyTh: 25, sellTh: -25, onlyOversold: tag === 'oversold', onlyOverheat: tag === 'overheat' });
  console.log(`${tag}: ${fmt(o)}`);
}

console.log('\n=== 5. 时间切分（2023前/后） ===');
for (const seg of [['2022-12-31', null, '2023前'], [null, '2023-01-01', '2023后']]) {
  const [to, from, label] = seg;
  const o = runBacktest({ buyTh: 25, sellTh: -25, toDate: to, fromDate: from });
  const bb = baseline(5, from, to);
  console.log(`${label}: 信号 ${fmt(o)} | 基线 上涨${bb.upRate != null ? bb.upRate.toFixed(2) + '%' : '-'} 均收${bb.avgRet != null ? (bb.avgRet * 100).toFixed(2) + '%' : '-'}`);
}
