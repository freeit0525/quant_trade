// ============ 股票查询历史（localStorage 本地保存，按查询次数排序） ============
// 用法:
//   QueryHistory.record(code, name)   记录一次查询（次数+1，刷新最近时间）
//   QueryHistory.list(limit)          返回 [{code, name, count, lastTime}]，按次数降序、次数相同按最近时间
//   QueryHistory.clear()              清空全部历史
(function () {
  const KEY = 'query_history_v1';

  function load() {
    try {
      const map = JSON.parse(localStorage.getItem(KEY)) || {};
      // 清理无效记录：A股代码必须为6位数字，防止历史坏数据（如 "undefined"）残留并继续传播
      let dirty = false;
      for (const k of Object.keys(map)) {
        const v = map[k] || {};
        if (!/^\d{6}$/.test(String(k).trim()) || !/^\d{6}$/.test(String(v.code || '').trim())) {
          delete map[k];
          dirty = true;
        }
      }
      if (dirty) save(map);
      return map;
    } catch (e) { return {}; }
  }

  function save(map) {
    try { localStorage.setItem(KEY, JSON.stringify(map)); } catch (e) { /* 存储不可用时静默忽略 */ }
  }

  function record(code, name) {
    // 仅记录合法A股代码（6位数字），避免把 undefined/空值等坏数据写入历史
    const key = String(code || '').trim();
    if (!/^\d{6}$/.test(key)) return;
    const map = load();
    const cur = map[key] || { name: '', count: 0, lastTime: 0 };
    cur.count = (cur.count || 0) + 1;
    cur.lastTime = Date.now();
    if (name) cur.name = name;
    map[key] = cur;
    save(map);
  }

  function list(limit) {
    const map = load();
    return Object.values(map)
      .sort((a, b) => (b.count - a.count) || (b.lastTime - a.lastTime))
      .slice(0, limit || 10);
  }

  function clear() {
    localStorage.removeItem(KEY);
  }

  window.QueryHistory = { record: record, list: list, clear: clear };
})();
