// ============================================================
// 买卖点预测评分引擎（共享，供 predict.html / predict-backtest.html 使用）
// 与 predict.html 中原内联逻辑保持一致；修改因子规则请同步此文件
// ============================================================

// ---------- 基础工具 ----------
function v(x) { return (x === null || x === undefined || x === '' || isNaN(x)) ? null : Number(x); }
function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
function fmtNum(x, d = 2) { return (x == null) ? '-' : Number(x).toFixed(d); }
function fmtWan(x) {
  x = Number(x);
  const a = Math.abs(x);
  if (a >= 1e8) return (x / 1e8).toFixed(2) + '亿';
  if (a >= 1e4) return (x / 1e4).toFixed(0) + '万';
  return x.toFixed(0);
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ============ 因子评分（-100 ~ +100，正利多负利空） ============
// 2026-08-12 因子降维：基本面因子(fund)已停用（权重0），实际生效 12 因子

// 1. 主力成交情况：主力净流入占成交额比例 + 近3日累计 + 趋势
function factorMainCap(rows, i) {
  const r = rows[i];
  const inflow = v(r.main_net_inflow), amount = v(r.amount);
  if (inflow == null || amount == null || amount <= 0) {
    return { score: 0, desc: ['主力资金流数据缺失（仅近期交易日有），不计分'] };
  }
  let score = 0;
  const desc = [];
  const pct = inflow / amount * 100;
  score += clamp(pct / 5, -1, 1) * 50;
  desc.push(`今日主力净流入 ${fmtWan(inflow)}，占成交额 ${pct.toFixed(2)}%`);
  let sumIn = 0, sumAmt = 0;
  for (let j = Math.max(0, i - 2); j <= i; j++) {
    const ri = rows[j];
    const fi = v(ri.main_net_inflow), ai = v(ri.amount);
    if (fi != null && ai != null) { sumIn += fi; sumAmt += ai; }
  }
  if (sumAmt > 0) {
    const cum = sumIn / sumAmt * 100;
    score += clamp(cum / 5, -1, 1) * 35;
    desc.push(`近3日主力净流入占成交额 ${cum.toFixed(2)}%`);
  }
  if (i >= 1) {
    const pr = rows[i - 1];
    const pIn = v(pr.main_net_inflow), pAmt = v(pr.amount);
    if (pIn != null && pAmt != null && pAmt > 0) {
      const pPct = pIn / pAmt * 100;
      if (pct > pPct) { score += 15; desc.push('主力净流入占比较上日放大'); }
      else { score -= 15; desc.push('主力净流入占比较上日缩小'); }
    }
  }
  return { score: clamp(score, -100, 100), desc };
}

// 2. 成交量：量能趋势（近5日均量 vs 近20日均量）× 涨跌方向
function factorVolume(rows, i) {
  const r = rows[i];
  const vol = v(r.volume);
  let pct = v(r.pct_change);
  if (vol == null || vol <= 0) return { score: 0, desc: ['成交量数据缺失，不计分'] };
  if (pct == null) pct = 0;
  // 近5日/近20日均量（含当日）
  let s5 = 0, n5 = 0, s20 = 0, n20 = 0;
  for (let j = Math.max(0, i - 4); j <= i; j++) { const x = v(rows[j].volume); if (x != null && x > 0) { s5 += x; n5++; } }
  for (let j = Math.max(0, i - 19); j <= i; j++) { const x = v(rows[j].volume); if (x != null && x > 0) { s20 += x; n20++; } }
  if (n5 === 0 || n20 === 0) return { score: 0, desc: ['成交量前置不足，不计分'] };
  const avg5 = s5 / n5, avg20 = s20 / n20;
  const ratio = avg5 / avg20; // >1 放量趋势，<1 缩量趋势
  let score = 0;
  const desc = [];
  desc.push(`近5日均量/近20日均量=${ratio.toFixed(2)}`);
  const trend = ratio >= 1.2 ? '明显放量' : ratio >= 1.05 ? '温和放量' : ratio <= 0.8 ? '明显缩量' : ratio <= 0.95 ? '温和缩量' : '量能平稳';
  if (pct > 0) {
    if (ratio >= 1.2) { score += 60; desc.push(`放量上涨（${trend}），资金持续参与`); }
    else if (ratio >= 1.05) { score += 30; desc.push(`温和放量上涨（${trend}）`); }
    else if (ratio <= 0.8) { score -= 40; desc.push(`缩量上涨（${trend}），上攻动能不足`); }
    else { score -= 10; desc.push(`量能平稳上涨（${trend}）`); }
  } else if (pct < 0) {
    if (ratio >= 1.2) { score -= 60; desc.push(`放量下跌（${trend}），抛压沉重`); }
    else if (ratio >= 1.05) { score -= 30; desc.push(`温和放量下跌（${trend}）`); }
    else if (ratio <= 0.8) { score += 40; desc.push(`缩量下跌（${trend}），卖压衰竭`); }
    else { score -= 10; desc.push(`量能平稳下跌（${trend}）`); }
  } else {
    desc.push(`平盘（${trend}），方向不明`);
  }
  return { score: clamp(score, -100, 100), desc };
}

// 3. 换手率：活跃度（过高过热、过低不活跃；温和活跃配合方向）
function factorTurnover(rows, i) {
  const r = rows[i];
  const to = v(r.turnover);
  let pct = v(r.pct_change);
  if (to == null) return { score: 0, desc: ['换手率数据缺失，不计分'] };
  if (pct == null) pct = 0;
  let score = 0;
  const desc = [];
  const up = pct > 0;
  if (to > 20) { score -= 45; desc.push(`换手率${to.toFixed(1)}% 过热（短线分歧极大，警惕见顶）`); }
  else if (to > 15) { score -= 30; desc.push(`换手率${to.toFixed(1)}% 高度活跃（${up ? '上涨' : '下跌'}，注意风险）`); }
  else if (to > 10) { score += up ? 25 : -25; desc.push(`换手率${to.toFixed(1)}% 高度活跃`); }
  else if (to > 5) { score += 40; desc.push(`换手率${to.toFixed(1)}% 活跃（短线人气佳）`); }
  else if (to > 3) { score += 25; desc.push(`换手率${to.toFixed(1)}% 温和活跃`); }
  else if (to > 1) { score += 5; desc.push(`换手率${to.toFixed(1)}% 交投一般`); }
  else { score -= 20; desc.push(`换手率${to.toFixed(1)}% 过于清淡（流动性差）`); }
  if (up && to >= 3 && to <= 10) { score += 10; desc.push('放量上涨+换手活跃，资金进场'); }
  if (!up && to >= 10) { score -= 15; desc.push('高换手下跌，警惕出货'); }
  return { score: clamp(score, -100, 100), desc };
}

// 4. 量比：当日放量程度 × 涨跌方向（放量上涨抢筹、放量下跌抛压、缩量下跌衰竭）
function factorVolRatio(rows, i) {
  const r = rows[i];
  const vr = v(r.volume_ratio);
  let pct = v(r.pct_change);
  if (vr == null) return { score: 0, desc: ['量比数据缺失，不计分'] };
  if (pct == null) pct = 0;
  let score = 0;
  const desc = [];
  const tag = `量比${vr.toFixed(2)} ${pct >= 0 ? '上涨' : '下跌'}${Math.abs(pct).toFixed(2)}%`;
  if (pct > 0) {
    if (vr >= 2.5) { score += 70; desc.push(`放量上涨（${tag}），资金抢筹`); }
    else if (vr >= 1.5) { score += 45; desc.push(`明显放量上涨（${tag}）`); }
    else if (vr >= 1.0) { score += 20; desc.push(`温和放量上涨（${tag}）`); }
    else { score -= 20; desc.push(`缩量上涨（${tag}），上攻动能不足`); }
  } else if (pct < 0) {
    if (vr >= 2.5) { score -= 70; desc.push(`放量下跌（${tag}），抛压沉重`); }
    else if (vr >= 1.5) { score -= 45; desc.push(`明显放量下跌（${tag}）`); }
    else if (vr >= 1.0) { score -= 20; desc.push(`温和放量下跌（${tag}）`); }
    else { score += 25; desc.push(`缩量下跌（${tag}），卖压趋缓`); }
  } else {
    if (vr >= 1.5) { score += 10; desc.push(`平盘放量（${tag}），多空分歧加大`); }
    else desc.push(`平盘缩量（${tag}），方向不明`);
  }
  return { score: clamp(score, -100, 100), desc };
}

// 5. RSI：超买超卖 + 金叉死叉 + 趋势
function factorRSI(rows, i) {
  const r = rows[i];
  const r6 = v(r.rsi6), r14 = v(r.rsi14);
  if (r6 == null) return { score: 0, desc: ['RSI数据缺失，不计分'] };
  let score = 0;
  const desc = [];
  if (r6 <= 20) { score += 60; desc.push(`RSI6=${r6.toFixed(1)} 深度超卖`); }
  else if (r6 <= 30) { score += 40; desc.push(`RSI6=${r6.toFixed(1)} 超卖区`); }
  else if (r6 <= 40) { score += 15; desc.push(`RSI6=${r6.toFixed(1)} 偏弱`); }
  else if (r6 >= 80) { score -= 60; desc.push(`RSI6=${r6.toFixed(1)} 深度超买`); }
  else if (r6 >= 70) { score -= 40; desc.push(`RSI6=${r6.toFixed(1)} 超买区`); }
  else if (r6 >= 60) { score -= 15; desc.push(`RSI6=${r6.toFixed(1)} 偏强`); }
  else desc.push(`RSI6=${r6.toFixed(1)} 中性`);
  if (i >= 1 && r14 != null) {
    const p6 = v(rows[i - 1].rsi6), p14 = v(rows[i - 1].rsi14);
    if (p6 != null && p14 != null) {
      if (p6 < p14 && r6 >= r14) { score += 25; desc.push('RSI6上穿RSI14 金叉'); }
      else if (p6 > p14 && r6 <= r14) { score -= 25; desc.push('RSI6下穿RSI14 死叉'); }
    }
    if (p6 != null) {
      if (r6 > p6) { score += 8; desc.push('RSI6上行'); }
      else if (r6 < p6) { score -= 8; desc.push('RSI6下行'); }
    }
  }
  return { score: clamp(score, -100, 100), desc };
}

// 6. KDJ：超买超卖 + 金叉死叉 + J值极值
function factorKDJ(rows, i) {
  const r = rows[i];
  const k = v(r.kdj_k), d = v(r.kdj_d), j = v(r.kdj_j);
  if (k == null || d == null) return { score: 0, desc: ['KDJ数据缺失，不计分'] };
  let score = 0;
  const desc = [];
  if (k <= 20) { score += 50; desc.push(`K=${k.toFixed(1)} 超卖`); }
  else if (k >= 80) { score -= 50; desc.push(`K=${k.toFixed(1)} 超买`); }
  else if (k <= 30) { score += 25; desc.push(`K=${k.toFixed(1)} 低位`); }
  else if (k >= 70) { score -= 25; desc.push(`K=${k.toFixed(1)} 高位`); }
  else desc.push(`K=${k.toFixed(1)} 中性`);
  if (j != null) {
    if (j <= 0) { score += 30; desc.push(`J=${j.toFixed(1)} 触底`); }
    else if (j >= 100) { score -= 30; desc.push(`J=${j.toFixed(1)} 超买极值`); }
  }
  if (i >= 1) {
    const pk = v(rows[i - 1].kdj_k), pd = v(rows[i - 1].kdj_d);
    if (pk != null && pd != null) {
      if (pk < pd && k >= d) { score += 30; desc.push('K上穿D 金叉'); }
      else if (pk > pd && k <= d) { score -= 30; desc.push('K下穿D 死叉'); }
    }
  }
  return { score: clamp(score, -100, 100), desc };
}

// 7. 乖离率：负乖离超跌利多，正乖离超买利空
function factorBias(rows, i) {
  const r = rows[i];
  const b5 = v(r.bias5), b10 = v(r.bias10), b20 = v(r.bias20);
  if (b5 == null && b10 == null && b20 == null) return { score: 0, desc: ['乖离率数据缺失，不计分'] };
  let score = 0;
  const desc = [];
  if (b5 != null) {
    if (b5 <= -5) { score += 50; desc.push(`BIAS5=${b5.toFixed(2)}% 深度负乖离(超跌)`); }
    else if (b5 <= -3) { score += 30; desc.push(`BIAS5=${b5.toFixed(2)}% 负乖离(超跌)`); }
    else if (b5 >= 6) { score -= 50; desc.push(`BIAS5=${b5.toFixed(2)}% 深度正乖离(超买)`); }
    else if (b5 >= 4) { score -= 30; desc.push(`BIAS5=${b5.toFixed(2)}% 正乖离(超买)`); }
  }
  if (b10 != null) {
    if (b10 <= -8) { score += 35; desc.push(`BIAS10=${b10.toFixed(2)}% 深度负乖离`); }
    else if (b10 <= -5) { score += 20; desc.push(`BIAS10=${b10.toFixed(2)}% 负乖离`); }
    else if (b10 >= 12) { score -= 35; desc.push(`BIAS10=${b10.toFixed(2)}% 深度正乖离`); }
    else if (b10 >= 8) { score -= 20; desc.push(`BIAS10=${b10.toFixed(2)}% 正乖离`); }
  }
  if (b20 != null) {
    if (b20 <= -12) { score += 25; desc.push(`BIAS20=${b20.toFixed(2)}% 深度负乖离`); }
    else if (b20 >= 16) { score -= 25; desc.push(`BIAS20=${b20.toFixed(2)}% 深度正乖离`); }
  }
  if (!desc.length) desc.push(`乖离率中性（BIAS5=${b5 != null ? b5.toFixed(2) : '-'}%）`);
  return { score: clamp(score, -100, 100), desc };
}

// 8. MACD：金叉状态 + 红绿柱 + 变化速率（MACD变大买/变小卖）
function factorMacd(rows, i) {
  const r = rows[i];
  const dif = v(r.macd_dif), dea = v(r.macd_dea), hist = v(r.macd_hist);
  if (dif == null || dea == null || hist == null) return { score: 0, desc: ['MACD数据缺失，不计分'] };
  let score = 0;
  const desc = [];
  if (dif >= dea) { score += 25; desc.push('DIF在DEA上方(多头)'); }
  else { score -= 25; desc.push('DIF在DEA下方(空头)'); }
  if (hist > 0) { score += 20; desc.push('MACD红柱'); }
  else if (hist < 0) { score -= 20; desc.push('MACD绿柱'); }
  else desc.push('MACD柱归零');
  if (i >= 1) {
    const ph = v(rows[i - 1].macd_hist);
    if (ph != null) {
      if (hist > ph) { score += 30; desc.push(`MACD变大(+${(hist - ph).toFixed(4)})，动能增强`); }
      else if (hist < ph) { score -= 30; desc.push(`MACD变小(${(hist - ph).toFixed(4)})，动能减弱`); }
    }
    const pdif = v(rows[i - 1].macd_dif), pdea = v(rows[i - 1].macd_dea);
    if (pdif != null && pdea != null) {
      if (pdif < pdea && dif >= dea) { score += 25; desc.push('MACD金叉'); }
      else if (pdif > pdea && dif <= dea) { score -= 25; desc.push('MACD死叉'); }
    }
  }
  return { score: clamp(score, -100, 100), desc };
}

// 10. K线形状：长阳长阴 + 影线 + 20日区间位置 + 连阳连阴
function factorCandle(rows, i) {
  if (i < 1) return { score: 0, desc: ['K线前置不足，不计分'] };
  const r = rows[i], pr = rows[i - 1];
  const prevClose = v(pr.close);
  const open = v(r.open), close = v(r.close), high = v(r.high), low = v(r.low);
  if (prevClose == null || open == null || close == null || high == null || low == null) {
    return { score: 0, desc: ['K线数据缺失，不计分'] };
  }
  let score = 0;
  const desc = [];
  const bodyPct = (close - open) / prevClose * 100;
  const rangePct = (high - low) / prevClose * 100;
  if (bodyPct >= 3) { score += 40; desc.push(`长阳线(${bodyPct.toFixed(2)}%)`); }
  else if (bodyPct <= -3) { score -= 40; desc.push(`长阴线(${bodyPct.toFixed(2)}%)`); }
  else if (Math.abs(bodyPct) < 0.5 && rangePct >= 3) desc.push('十字星(多空犹豫)');
  const range = high - low;
  if (range > 0) {
    const upper = high - Math.max(open, close);
    const lower = Math.min(open, close) - low;
    if (upper / range >= 0.6) { score -= 25; desc.push('长上影线(上方抛压)'); }
    if (lower / range >= 0.6) { score += 25; desc.push('长下影线(下方支撑)'); }
  }
  const start = Math.max(0, i - 19);
  let hi20 = -Infinity, lo20 = Infinity;
  for (let j = start; j <= i; j++) {
    const h = v(rows[j].high), l = v(rows[j].low);
    if (h != null) hi20 = Math.max(hi20, h);
    if (l != null) lo20 = Math.min(lo20, l);
  }
  if (isFinite(hi20) && isFinite(lo20) && hi20 > lo20) {
    const pos = (close - lo20) / (hi20 - lo20);
    if (pos >= 0.9) { score -= 20; desc.push('收盘处于20日高位'); }
    else if (pos <= 0.1) { score += 20; desc.push('收盘处于20日低位'); }
  }
  let up = v(r.pct_change) >= 0, streak = 1;
  for (let j = i - 1; j >= 0; j--) {
    const pc = v(rows[j].pct_change);
    if (pc == null) break;
    if ((pc >= 0) === up) streak++; else break;
  }
  if (up && streak >= 3) { score += 15; desc.push(`连阳${streak}日(强势延续)`); }
  else if (!up && streak >= 3) { score -= 15; desc.push(`连阴${streak}日(弱势延续)`); }
  if (!desc.length) desc.push('K线形态中性');
  return { score: clamp(score, -100, 100), desc };
}

// 9. 均线系统 MA5/MA10/MA20/MA30：排列 + 站上跌破 + 金叉死叉 + 趋势
function factorMA(rows, i) {
  const r = rows[i];
  const close = v(r.close);
  const ma5 = v(r.ma5), ma10 = v(r.ma10), ma20 = v(r.ma20), ma30 = v(r.ma30);
  if (close == null || ma5 == null || ma10 == null || ma20 == null || ma30 == null) {
    return { score: 0, desc: ['均线数据缺失（需30日以上数据），不计分'] };
  }
  let score = 0;
  const desc = [];
  const arr = [ma5, ma10, ma20, ma30];
  const sorted = [...arr].sort((a, b) => b - a);
  const isBull = arr.every((v2, k) => v2 === sorted[k]); // MA5>MA10>MA20>MA30
  const isBear = arr.every((v2, k) => v2 === sorted[3 - k]);
  if (isBull) { score += 40; desc.push('均线多头排列(MA5>MA10>MA20>MA30)'); }
  else if (isBear) { score -= 40; desc.push('均线空头排列(MA5<MA10<MA20<MA30)'); }
  // 收盘价与各均线位置
  const names = ['MA5', 'MA10', 'MA20', 'MA30'];
  const vals = [ma5, ma10, ma20, ma30];
  let above = 0;
  vals.forEach((m, k) => { if (close > m) above++; });
  desc.push(`收盘价站上${above}条均线`);
  score += (above - 2) * 12; // 站上越多越利多
  if (i >= 1) {
    const p = rows[i - 1];
    const p5 = v(p.ma5), p10 = v(p.ma10), p20 = v(p.ma20);
    if (p5 != null && p10 != null) {
      if (p5 < p10 && ma5 >= ma10) { score += 20; desc.push('MA5上穿MA10 金叉'); }
      else if (p5 > p10 && ma5 <= ma10) { score -= 20; desc.push('MA5下穿MA10 死叉'); }
    }
    if (p5 != null && p20 != null) {
      if (p5 < p20 && ma5 >= ma20) { score += 15; desc.push('MA5上穿MA20 金叉'); }
      else if (p5 > p20 && ma5 <= ma20) { score -= 15; desc.push('MA5下穿MA20 死叉'); }
    }
    if (p5 != null) {
      if (ma5 > p5) { score += 8; desc.push('MA5上行'); }
      else if (ma5 < p5) { score -= 8; desc.push('MA5下行'); }
    }
  }
  if (!desc.length) desc.push('均线系统中性');
  return { score: clamp(score, -100, 100), desc };
}

// 12. OBV 能量潮：量价累积资金流向（顶/底背离 + 趋势 + 金叉死叉 + 近5日斜率）
// 性能优化：若 rows 已附加 __obv 预计算序列（回测页全量循环时自动附加），直接复用；
// 否则按原逻辑从 0 累加重建（与 predict.html 行为完全一致）
function factorOBV(rows, i) {
  if (i < 20) return { score: 0, desc: ['OBV需20日以上数据，不计分'] };
  // OBV 序列：收涨加量、收跌减量、平盘不变
  const obv = rows.__obv || (function () {
    const a = [];
    let val = 0;
    for (let j = 0; j < rows.length; j++) {
      const c = v(rows[j].close);
      const pc = j > 0 ? v(rows[j - 1].close) : null;
      const vol = v(rows[j].volume) || 0;
      if (c != null && pc != null && vol > 0) {
        if (c > pc) val += vol;
        else if (c < pc) val -= vol;
      }
      a.push(val);
    }
    return a;
  })();
  const cur = obv[i];
  let score = 0;
  const desc = [];

  // 近20日均线（含当日）与金叉死叉
  let s20 = 0;
  for (let j = Math.max(0, i - 19); j <= i; j++) s20 += obv[j];
  const ma20 = s20 / Math.min(20, i + 1);
  if (cur > ma20) { score += 12; desc.push('OBV站上20日均线，资金净流入趋势'); }
  else if (cur < ma20) { score -= 12; desc.push('OBV跌破20日均线，资金净流出趋势'); }
  if (i >= 1) {
    let s20p = 0;
    for (let j = Math.max(0, i - 20); j < i; j++) s20p += obv[j];
    const ma20p = s20p / Math.min(20, i);
    if (obv[i - 1] <= ma20p && cur > ma20) { score += 20; desc.push('OBV上穿20日均线(金叉)，资金进场'); }
    else if (obv[i - 1] >= ma20p && cur < ma20) { score -= 20; desc.push('OBV下穿20日均线(死叉)，资金离场'); }
  }

  // 顶/底背离与创新高/低确认（对比此前近20日高低点）
  let obvHi = -Infinity, obvLo = Infinity, priceHi = -Infinity, priceLo = -Infinity;
  for (let j = Math.max(0, i - 19); j < i; j++) {
    obvHi = Math.max(obvHi, obv[j]);
    obvLo = Math.min(obvLo, obv[j]);
    const h = v(rows[j].high), l = v(rows[j].low);
    if (h != null) priceHi = Math.max(priceHi, h);
    if (l != null) priceLo = Math.min(priceLo, l);
  }
  const c = v(rows[i].close);
  if (c != null && isFinite(priceHi) && isFinite(priceLo) && isFinite(obvHi) && isFinite(obvLo)) {
    if (c > priceHi && cur <= obvHi) { score -= 35; desc.push('价格创新高但OBV未跟进(顶背离)，上涨动力存疑'); }
    if (c < priceLo && cur >= obvLo) { score += 35; desc.push('价格创新低但OBV未创新低(底背离)，下跌动力衰竭'); }
    if (c <= priceHi && cur > obvHi) { score += 20; desc.push('OBV创新高而价格未创新高，强势确认'); }
    if (c >= priceLo && cur < obvLo) { score -= 20; desc.push('OBV创新低而价格未创新低，弱势确认'); }
  }

  // 近5日 OBV 斜率（按5日均量归一化）
  let s5 = 0, n5 = 0;
  for (let j = Math.max(0, i - 4); j <= i; j++) { const x = v(rows[j].volume); if (x != null && x > 0) { s5 += x; n5++; } }
  const avgVol = n5 ? s5 / n5 : 1;
  if (avgVol > 0) {
    const delta5 = cur - obv[Math.max(0, i - 5)];
    const k = delta5 / avgVol;
    if (k >= 3) { score += 15; desc.push(`近5日OBV上升(${k.toFixed(1)}日均量)，资金持续流入`); }
    else if (k <= -3) { score -= 15; desc.push(`近5日OBV下降(${Math.abs(k).toFixed(1)}日均量)，资金持续流出`); }
    else desc.push(`近5日OBV变化不大(${k.toFixed(1)}日均量)`);
  }
  if (!desc.length) desc.push('OBV中性');
  return { score: clamp(score, -100, 100), desc };
}

// 13. 近5日涨跌幅极值（动量反转：超跌反弹 / 超涨回落，A股短线实测最有效的信号）
function factorMom5(rows, i) {
  const c0 = v(rows[i].close);
  const c5 = i >= 5 ? v(rows[i - 5].close) : null;
  if (c0 == null || c0 <= 0 || c5 == null || c5 <= 0) {
    return { score: 0, desc: ['近5日涨跌幅数据不足（需5日以上数据），不计分'] };
  }
  const ret = c0 / c5 - 1;
  let score = 0;
  const desc = [];
  const retTxt = `${(ret * 100).toFixed(2)}%`;
  if (ret <= -0.10) { score = 90; desc.push(`近5日累计跌${retTxt}，深度超跌，反弹概率高`); }
  else if (ret <= -0.08) { score = 75; desc.push(`近5日累计跌${retTxt}，明显超跌，超跌反弹机会大`); }
  else if (ret <= -0.06) { score = 55; desc.push(`近5日累计跌${retTxt}，超跌待反弹`); }
  else if (ret <= -0.04) { score = 35; desc.push(`近5日累计跌${retTxt}，短期回调较深，存在反弹需求`); }
  else if (ret <= -0.02) { score = 15; desc.push(`近5日累计跌${retTxt}，小幅回调`); }
  else if (ret >= 0.16) { score = -90; desc.push(`近5日累计涨${retTxt}，涨幅过大，短线回调压力大`); }
  else if (ret >= 0.12) { score = -70; desc.push(`近5日累计涨${retTxt}，短线涨幅过大，警惕回落`); }
  else if (ret >= 0.09) { score = -50; desc.push(`近5日累计涨${retTxt}，涨幅偏大，追高风险高`); }
  else if (ret >= 0.06) { score = -30; desc.push(`近5日累计涨${retTxt}，短期涨速较快`); }
  else if (ret >= 0.04) { score = -15; desc.push(`近5日累计涨${retTxt}，涨势温和`); }
  else desc.push(`近5日累计涨跌${retTxt}，处于合理区间`);
  return { score: clamp(score, -100, 100), desc };
}

// 因子定义表（顺序固定；desc 为详细打分规则，点击因子可查看）
const FACTOR_DEFS = [
  { key: 'main', name: '主力成交', calc: factorMainCap,
    desc: '主力净流入占成交额比例打分：\n· 今日占比每 5% 计 ±50 分（净流入为正）\n· 近3日累计占比每 5% 计 ±35 分\n· 较上日放大 +15 / 缩小 -15\n注：数据缺失（仅近期交易日有）不计分' },
  { key: 'vol', name: '成交量', calc: factorVolume,
    desc: '量能趋势=近5日均量÷近20日均量：\n· ≥1.2 明显放量：上涨 +60 / 下跌 -60\n· 1.05~1.2 温和放量：上涨 +30 / 下跌 -30\n· 0.8~1.05 量能平稳：上涨 -10 / 下跌 -10\n· ≤0.8 明显缩量：上涨 -40 / 下跌 +40(卖压衰竭)' },
  { key: 'turnover', name: '换手率', calc: factorTurnover,
    desc: '换手活跃度打分（越高越活跃）：\n· >20% -45 过热，短线分歧极大，警惕见顶\n· 15~20% -30 高度活跃，注意风险\n· 10~15% ±25 高度活跃，跟随涨跌方向\n· 5~10% +40 活跃，短线人气佳\n· 3~5% +25 温和活跃\n· 1~3% +5 交投一般\n· ≤1% -20 过于清淡，流动性差\n加项：放量上涨+换手3~10% +10；高换手下跌≥10% -15(警惕出货)' },
  { key: 'volratio', name: '量比', calc: factorVolRatio,
    desc: '当日量比×涨跌方向（放量配合方向才有效）：\n· 放量上涨抢筹：量比≥2.5 +70 / ≥1.5 +45 / ≥1.0 +20 / 缩量上涨 -20\n· 放量下跌抛压：量比≥2.5 -70 / ≥1.5 -45 / ≥1.0 -20 / 缩量下跌 +25(卖压趋缓)\n· 平盘：放量(≥1.5) +10，多空分歧加大' },
  { key: 'rsi', name: 'RSI', calc: factorRSI,
    desc: 'RSI6 超买超卖：\n· ≤20 深度超卖 +60 / ≤30 +40 / ≤40 +15\n· ≥80 深度超买 -60 / ≥70 -40 / ≥60 -15\n· 其余中性\n交叉：RSI6上穿RSI14金叉 +25 / 下穿死叉 -25\n趋势：RSI6上行 +8 / 下行 -8' },
  { key: 'kdj', name: 'KDJ', calc: factorKDJ,
    desc: 'K值区间：≤20 +50 / ≤30 +25 / ≥70 -25 / ≥80 -50\nJ值极值：≤0 触底 +30 / ≥100 超买极值 -30\n交叉：K上穿D金叉 +30 / K下穿D死叉 -30' },
  { key: 'bias', name: '乖离率', calc: factorBias,
    desc: '负乖离超跌利多 / 正乖离超买利空：\n· BIAS5：≤-5 +50 / ≤-3 +30 / ≥4 -30 / ≥6 -50\n· BIAS10：≤-8 +35 / ≤-5 +20 / ≥8 -20 / ≥12 -35\n· BIAS20：≤-12 +25 / ≥16 -25' },
  { key: 'macd', name: 'MACD', calc: factorMacd,
    desc: '状态：DIF在DEA上方(多头) +25 / 下方(空头) -25\n柱体：红柱 +20 / 绿柱 -20\n变化速率：柱体较上日变大 +30(动能增强) / 变小 -30\n交叉：金叉 +25 / 死叉 -25' },
  { key: 'candle', name: 'K线形状', calc: factorCandle,
    desc: '实体：长阳(≥3%) +40 / 长阴(≤-3%) -40\n影线：长上影(≥60%振幅) -25 / 长下影(≥60%振幅) +25\n位置：收盘20日高位(≥90%) -20 / 低位(≤10%) +20\n延续：连阳≥3日 +15 / 连阴≥3日 -15' },
  { key: 'ma', name: '均线', calc: factorMA,
    desc: '排列：多头排列(MA5>MA10>MA20>MA30) +40 / 空头排列 -40\n站上：每多站上1条均线 +12（基准2条，站上越多越强）\n交叉：MA5上穿MA10 +20 / 下穿 -20；上穿MA20 +15 / 下穿 -15\n趋势：MA5上行 +8 / 下行 -8' },
  { key: 'obv', name: 'OBV', calc: factorOBV,
    desc: 'OBV=收涨累加量/收跌累减量的能量潮指标：\n· 站上20日均线 +12 / 跌破 -12；上穿金叉 +20 / 下穿死叉 -20\n· 顶背离(价创新高OBV未跟上) -35 / 底背离(价创新低OBV未新低) +35\n· OBV创新高而价未新高 +20 / 反向 -20\n· 近5日OBV变化≥3日均量 +15 / ≤-3 -15' },
  { key: 'fund', name: '基本面', calc: null,
    desc: '【2026-08-12 已停用，权重0】流通市值（来自 stock_info）：\n· 30~300亿(活跃适中) +30\n· 10~30亿(小盘活跃) +15\n· 300~1000亿(中大盘) +10\n· >1000亿(大盘权重，短线难动) -20\n· <10亿(微盘，风险高) -10\n上市年限：<1年次新 +15 / 1~5年 +8 / 成熟0分\n停用原因：纯静态因子，无时间变化，不参与"评分跃升"信号，且 IC 反向(-12‰)，剔除后全库命中率 60.4%→62.8%' },
  { key: 'mom5', name: '短期涨跌幅', calc: factorMom5,
    desc: '近5日累计涨跌幅（动量反转，超跌反弹/超涨回落）：\n· 跌≥10% +90 / 跌≥8% +75 / 跌≥6% +55 / 跌≥4% +35 / 跌≥2% +15\n· 涨≥16% -90 / 涨≥12% -70 / 涨≥9% -50 / 涨≥6% -30 / 涨≥4% -15\n注：基于全库回测，超跌反弹与超涨回落是A股短线最有效的信号' },
];

// 因子分组（仅展示层元数据，不影响评分与权重；2026-08-12 依据因子 IC 与相关性矩阵分析）
// 实验验证（合并高相关组 C0~C4，50只全池）：去重/等权/IC加权都会显著损失信号
// （差异化权重是信号形态的真实结构），故评分层不做合并；仅前端按组分开展示提升可解释性。
const FACTOR_GROUPS = [
  { key: 'oversold', name: '超买超卖组', keys: ['rsi', 'kdj', 'bias', 'mom5'],
    desc: '超买超卖类（组内相关 0.47~0.80）：RSI/KDJ 超买超卖与金叉死叉、乖离率超跌超涨、短期涨跌幅动量反转。权重差异不可合并，剔除任一都会降低命中率' },
  { key: 'volume', name: '量能组', keys: ['vol', 'turnover', 'volratio'],
    desc: '量能类（组内相关 0.3~0.5）：成交量趋势、换手活跃度、量比' },
  { key: 'trend', name: '趋势组', keys: ['macd', 'ma', 'candle'],
    desc: '趋势与形态类：MACD 金叉/柱体/速率、均线排列、K线形态' },
  { key: 'fundflow', name: '资金组', keys: ['main', 'obv'],
    desc: '资金流类：主力净流入占成交额、OBV 能量潮' },
];
// 未归组因子自动排在最后展示（已停用的 fund 权重0、calc=null，不渲染不计算）

// 2026-08-11 依据全库回测（9股约5万根K线）的命中率与因子有效性调整：
// - 新增"短期涨跌幅"因子(mom5)：近5日跌≥8%后买入，5日盘中触及+2%概率77.5%（基准47.5%）
// - 主力成交/MACD/均线 与命中负相关 → 降权；KDJ/乖离率/RSI 有效 → 保持升权
// 2026-08-12 因子降维（50只全池+沪深300 回测验证）：
// - 剔除"基本面"因子(fund)：纯静态因子（市值/上市年限）无时间变化，不参与"评分跃升"信号形态，
//   且 IC 反向(-12‰)。剔除后买入命中率 60.4%→62.8%（超额 11.3→13.7pt）、样本外(2023后) 53.4%→55.7% 同升，
//   策略总收益 +124pt、Calmar 0.33→0.35（叠加市场波动率过滤后 0.42），稳健非过拟合
// - 其余 12 因子全部保留：超买超卖组(rsi/kdj/bias/mom5)虽相关性高(0.47~0.80)且IC为负，
//   但实测剔除任意一个都会降低命中率（它们共同支撑"组合评分跃升上穿阈值"的信号形态），不能简单去重
// - 13→12 因子，权重如下（fund 权重 0 = 停用）
const DEFAULT_WEIGHTS = { main: 10, vol: 12, turnover: 10, volratio: 10, rsi: 12, kdj: 20, bias: 22, macd: 8, candle: 10, ma: 6, obv: 10, fund: 0, mom5: 22 };

// 卖出向独立权重：加大短期涨跌幅(mom5, 权重24)与超卖指标的卖出信号贡献，
// 全库回测卖出信号（5日盘中触及-2%）命中率由 63.4% 提升至 77.2%
// 2026-08-12 同步剔除基本面因子（同买入向，fund 权重 0 = 停用）
const DEFAULT_WEIGHTS_SELL = { main: 16, vol: 12, turnover: 12, volratio: 10, rsi: 14, kdj: 12, bias: 12, macd: 10, candle: 10, ma: 8, obv: 8, fund: 0, mom5: 24 };

// ============ 综合评分（双向独立权重） ============
// 12 因子打分只算一次，买入/卖出方向分别用各自权重加权，S ∈ [-100, 100]
// 已停用的因子（fund calc=null，权重0）不参与计算也不进入明细
function computeParts(rows, i, info) {
  return FACTOR_DEFS.filter(f => f.calc != null).map(f => {
    const res = f.calc(rows, i);
    return { key: f.key, name: f.name, ...res };
  });
}
function weightedS(parts, weights) {
  let totalW = 0, sum = 0;
  parts.forEach(p => { const w = weights[p.key] || 0; totalW += w; sum += w * p.score; });
  return totalW > 0 ? clamp(sum / totalW, -100, 100) : 0;
}
function scoreAtDual(rows, i, weightsBuy, weightsSell, info) {
  const parts = computeParts(rows, i, info);
  return { Sbuy: weightedS(parts, weightsBuy), Ssell: weightedS(parts, weightsSell), parts };
}

// ============ 综合操作判定（下一个交易日操作建议） ============
// 核心目标：预测下一个交易日该如何操作。
// 通过过去的交易日数据（13因子买入/卖出双权重评分 Sbuy/Ssell），输出单一操作建议：
// 加仓(add) / 持有(hold) / 观望(watch) / 减仓(trim) / 卖出(sell)
const OP_DEFS = {
  add:   { action: '加仓',   sub: '偏多信号强，可分批买入', cls: 'up',   color: '#e8453c' },
  hold:  { action: '持有',   sub: '趋势偏多，持股待涨',     cls: 'up',   color: '#e0873c' },
  watch: { action: '观望',   sub: '方向不明，等待方向确认', cls: 'hold', color: '#9e9e9e' },
  trim:  { action: '减仓',   sub: '偏空，分批减仓',         cls: 'down', color: '#7db45a' },
  sell:  { action: '卖出',   sub: '偏空信号强，清仓离场',   cls: 'down', color: '#34a853' },
};
function opVerdict(Sbuy, Ssell) {
  // 强买：买入向强 + 卖出向不冲突 → 加仓
  if (Sbuy >= 25 && Ssell >= -10) return { code: 'add', ...OP_DEFS.add };
  // 中买：买入向较强 + 卖出向无明显空头 → 轻仓加仓
  if (Sbuy >= 12 && Ssell >= -15) return { code: 'add', action: '加仓', sub: '偏多，可轻仓介入', cls: 'up', color: '#e8453c' };
  // 偏多：买入向为正 + 卖出向未转空 → 持有
  if (Sbuy >= 5 && Ssell > -12) return { code: 'hold', ...OP_DEFS.hold };
  // 强卖：卖出向强 或 买入向极空 → 卖出
  if (Sbuy <= -25 || Ssell <= -25) return { code: 'sell', ...OP_DEFS.sell };
  // 偏空：卖出向转空 或 买入向明显为负 → 减仓
  if (Sbuy <= -12 || Ssell <= -10) return { code: 'trim', ...OP_DEFS.trim };
  // 其余 → 观望
  return { code: 'watch', ...OP_DEFS.watch };
}

// 支撑位 / 压力位
function calcLevels(rows, i) {
  const r = rows[i];
  const close = v(r.close);
  const start = Math.max(0, i - 19);
  let hi20 = -Infinity, lo20 = Infinity;
  for (let j = start; j <= i; j++) {
    const h = v(rows[j].high), l = v(rows[j].low);
    if (h != null) hi20 = Math.max(hi20, h);
    if (l != null) lo20 = Math.min(lo20, l);
  }
  const cands = [['MA5', v(r.ma5)], ['MA10', v(r.ma10)], ['MA20', v(r.ma20)], ['20日低', lo20]];
  const supports = cands.filter(([, m]) => m != null && m < close).sort((a, b) => b[1] - a[1]);
  const resistCands = cands.concat([['20日高', hi20]]);
  const resists = resistCands.filter(([, m]) => m != null && m > close).sort((a, b) => a[1] - b[1]);
  const s1 = supports.length ? supports[0] : null;
  const s2 = supports.length > 1 ? supports[1] : null;
  const r1 = resists.length ? resists[0] : null;
  const r2 = resists.length > 1 ? resists[1] : null;
  return {
    s1: s1 ? s1[1] : (close ? close * 0.97 : null),
    s1Name: s1 ? s1[0] : '3%回撤',
    s2: s2 ? s2[1] : null,
    s2Name: s2 ? s2[0] : null,
    r1: r1 ? r1[1] : (close ? close * 1.05 : null),
    r1Name: r1 ? r1[0] : '5%目标',
    r2: r2 ? r2[1] : null,
    r2Name: r2 ? r2[0] : null,
    hi20: isFinite(hi20) ? hi20 : null,
    lo20: isFinite(lo20) ? lo20 : null,
  };
}

// 买卖时机建议文案
function timingAdvice(S, L) {
  const s1 = L.s1, r1 = L.r1;
  const stop = s1 ? Math.round(s1 * 0.97 * 100) / 100 : null;
  if (S >= 15) {
    return `回踩 ${L.s1Name}(${fmtNum(s1)}) 企稳可分批买入（首批1/3），放量突破 ${L.r1Name}(${fmtNum(r1)}) 可加仓；跌破 ${fmtNum(stop)} 止损`;
  }
  if (S <= -15) {
    return `反弹至 ${L.r1Name}(${fmtNum(r1)}) 附近分批减仓（先卖1/2），剩余仓位跌破 ${fmtNum(s1)} 全部离场`;
  }
  return `等待 ${fmtNum(s1)} 附近企稳信号或放量突破 ${fmtNum(r1)} 再介入，跌破 ${fmtNum(stop)} 观望回避`;
}

// ============ 历史信号（回看模型信号 + 命中统计） ============
// 2026-08-12 依据 50 只全池后5日收益率回测调优：
// BUY_TH 25→35、SELL_TH -25→-35 后，买入信号后5日收益率命中率 53.2%→60.4%（基线 49.1%）、
// 均收益 0.96%→1.79%；卖出信号后5日均收益 -0.68%（基线 +0.55%）；信号数约减半（1721→444），
// 策略级总收益 +170%→+340%。超跌/超涨过滤与单纯调权重提升有限，提高门槛最有效。
const BUY_TH = 35, SELL_TH = -35;
// 双向独立判定：买入信号看买入权重评分(Sbuy)由低位跃升，卖出信号看卖出权重评分(Ssell)由高位回落
// buyTh/sellTh 可自定义（默认取引擎调优阈值），predict-strategy.html 等页面可传入用户参数
// opts 可选：{ marketStates: Map(date->state), marketCfg: {enabled,volMin,volMax} }
//   传入 marketStates 后，买入信号默认启用大盘波动率过滤（DEFAULT_MARKET_CFG，可用 marketCfg 覆盖；
//   指数数据缺失日期视为通过）；卖出信号不过滤（高波动/弱势市卖出信号更需保留）。
function computeSignalsDual(rows, weightsBuy, weightsSell, info, buyTh = BUY_TH, sellTh = SELL_TH, opts) {
  const signals = [];
  const states = opts && opts.marketStates;
  const mcfg = (opts && opts.marketCfg) || DEFAULT_MARKET_CFG;
  let prevBuy = null, prevSell = null;
  for (let t = 40; t < rows.length; t++) {
    const { Sbuy, Ssell } = scoreAtDual(rows, t, weightsBuy, weightsSell, info);
    if (prevBuy != null && Sbuy >= buyTh && prevBuy < buyTh - 8) {
      if (!states || marketFilterAllow(states.get(rows[t].date), mcfg)) {
        signals.push({ t, type: 'buy', S: Sbuy });
      }
    }
    if (prevSell != null && Ssell <= sellTh && prevSell > sellTh + 8) {
      signals.push({ t, type: 'sell', S: Ssell });
    }
    prevBuy = Sbuy;
    prevSell = Ssell;
  }
  return signals;
}

// 历史信号命中统计（2026-08-12 口径调整）：
// - 主口径（后5日收益率，权威）：买入信号后 5 日收盘上涨=命中，卖出信号后 5 日收盘下跌=命中，
//   并输出 5 日平均收益（买卖信号都统计；卖出信号 5 日均收益为负=有效）
// - 辅助口径（盘中触及）：买入信号后 5 日内盘中最高触及 +2%=短线上涨概率，
//   卖出信号后 5 日内盘中最低触及 -2%=短线回落概率（对高波动股天然偏高，仅参考）
function signalStats(rows, signals) {
  let buyWin = 0, buyCnt = 0, sellWin = 0, sellCnt = 0;
  let buyTouch = 0, sellTouch = 0;
  const buyRets = [], sellRets = [];
  signals.forEach(sg => {
    const t = sg.t;
    if (t + 5 < rows.length) {
      const c0 = rows[t].close, c5 = rows[t + 5].close;
      const ret = (c0 > 0 && c5 != null) ? c5 / c0 - 1 : null;
      sg.ret5 = ret;
      // 触及口径：统计信号后5日内盘中最高/最低相对信号日收盘
      let hiMax = -Infinity, loMin = Infinity;
      for (let j = t + 1; j <= t + 5; j++) {
        if (rows[j].high != null) hiMax = Math.max(hiMax, rows[j].high);
        if (rows[j].low != null) loMin = Math.min(loMin, rows[j].low);
      }
      const touchUp = isFinite(hiMax) ? hiMax / rows[t].close - 1 : null;
      const touchDown = isFinite(loMin) ? loMin / rows[t].close - 1 : null;
      sg.touchUp = touchUp;
      sg.touchDown = touchDown;
      if (sg.type === 'buy') {
        buyCnt++;
        if (ret != null) { if (ret > 0) buyWin++; buyRets.push(ret); }
        if (touchUp != null && touchUp >= 0.02) buyTouch++;
      }
      else {
        sellCnt++;
        if (ret != null) { if (ret < 0) sellWin++; sellRets.push(ret); }
        if (touchDown != null && touchDown <= -0.02) sellTouch++;
      }
    }
  });
  const agg = arr => {
    if (!arr.length) return { n: 0, avg: null, med: null, hit: null };
    const sorted = [...arr].sort((a, b) => a - b);
    const avg = arr.reduce((s, x) => s + x, 0) / arr.length;
    const med = sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
    return { n: arr.length, avg: avg * 100, med: med * 100, hit: arr.filter(x => x > 0).length / arr.length * 100 };
  };
  return { buyWin, buyCnt, sellWin, sellCnt, buyTouch, sellTouch, buy: agg(buyRets), sell: agg(sellRets) };
}

// 随机入场基线：全历史任意一天（含数据且非信号日）后 N 日收益率统计，用于与信号命中率对比
// 返回 { hitUp: 后N日收盘上涨占比, avgRet: 平均收益率(%) }
function baselineStats(rows, evalDays = 5) {
  let cnt = 0, up = 0, sum = 0;
  for (let t = 40; t + evalDays < rows.length; t++) {
    const c0 = rows[t].close, cn = rows[t + evalDays].close;
    if (c0 == null || c0 <= 0 || cn == null) continue;
    const r = cn / c0 - 1;
    cnt++; if (r > 0) up++; sum += r;
  }
  return { n: cnt, hitUp: cnt ? up / cnt * 100 : null, avgRet: cnt ? sum / cnt * 100 : null };
}

// ============ 大盘情绪/波动率市场过滤（2026-08-12 新增） ============
// 依据：50 只全池 + 沪深300 指数回测（后5日收益率口径 + 完整资金曲线）。
// - 波动率过滤（年化波动率适中 15%~45%）：全样本 Calmar 0.28→0.47、回撤 28.4%→20.7%、年化 7.98%→9.72%；
//   样本外(2023后) Calmar 0.14→0.19 仍优于不过滤，稳健有效，落地为默认过滤。
// - 趋势过滤（站上MA20/近5日动量）：样本外命中 71%+（超额20pt）但样本内信号过少（444→39），
//   不作硬过滤，由页面展示大盘状态供参考。
// - 指数数据缺失的日期（早于指数起点等）视为通过（回退兼容旧行为）。

// 输入指数日K线（至少含 date/close），输出 Map(date -> {momentum5, aboveMA20, vol20})
function computeMarketState(indexRows) {
  const byDate = new Map();
  if (!indexRows || indexRows.length < 21) return byDate;
  for (let i = 20; i < indexRows.length; i++) {
    const c = indexRows[i].close, c5 = indexRows[i - 5].close;
    if (c == null || c5 == null || c5 <= 0) continue;
    let maSum = 0, ok = true;
    for (let j = i - 19; j <= i; j++) {
      const v = indexRows[j].close;
      if (v == null) { ok = false; break; }
      maSum += v;
    }
    if (!ok) continue;
    const ma20 = maSum / 20;
    const rets = [];
    for (let j = i - 19; j <= i; j++) {
      const pc = indexRows[j - 1].close, cc = indexRows[j].close;
      if (pc != null && cc != null && pc > 0) rets.push(cc / pc - 1);
    }
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = rets.length > 1 ? Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1)) : 0;
    byDate.set(indexRows[i].date, { momentum5: c / c5 - 1, aboveMA20: c > ma20, vol20: sd * Math.sqrt(252) });
  }
  return byDate;
}

// 默认市场过滤配置：大盘年化波动率适中区间（过低=无量行情、过高=过热风险市）
const DEFAULT_MARKET_CFG = { enabled: true, volMin: 0.15, volMax: 0.45 };
function marketFilterAllow(st, cfg) {
  if (!cfg || !cfg.enabled) return true;
  if (!st || st.vol20 == null) return true; // 指数数据缺失日不过滤
  if (cfg.volMin != null && st.vol20 < cfg.volMin) return false;
  if (cfg.volMax != null && st.vol20 > cfg.volMax) return false;
  return true;
}
