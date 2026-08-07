// ============ 可搜索下拉组件（输入筛选 + 下拉选择） ============
// 用法:
//   StockPicker.init(containerEl, stocks, { onSelect });
//   stocks: [{ Code, Name }]
//   onSelect(code, name): 选中某一项时回调
// 读取当前值:
//   StockPicker.getValue() -> { code, name } | null
//   StockPicker.getInput() -> 输入框当前文本
(function () {
  function init(container, stocks, opts) {
    opts = opts || {};
    const items = (stocks || []).map(s => ({ code: String(s.Code), name: s.Name || '' }));

    container.classList.add('picker');
    container.innerHTML =
      '<input type="text" class="picker-input" placeholder="输入代码或名称筛选" autocomplete="off">' +
      '<span class="picker-arrow">▼</span>' +
      '<div class="picker-list"></div>';

    const input = container.querySelector('.picker-input');
    const list = container.querySelector('.picker-list');
    const state = { code: '', name: '' };

    function render(filter) {
      const f = (filter || '').trim().toLowerCase();
      let matched = f
        ? items.filter(it => it.code.includes(f) || it.name.toLowerCase().includes(f))
        : items;
      matched = matched.slice(0, 500);
      if (matched.length === 0) {
        list.innerHTML = '<div class="picker-empty">无匹配项</div>';
      } else {
        list.innerHTML = matched.map(it =>
          `<div class="picker-item" data-code="${it.code}" data-name="${it.name.replace(/"/g, '&quot;')}">${it.name} (${it.code})</div>`
        ).join('');
      }
    }

    function show() { list.style.display = 'block'; }
    function hide() { list.style.display = 'none'; }

    function pick(itemEl) {
      const code = itemEl.dataset.code;
      const name = itemEl.dataset.name;
      state.code = code;
      state.name = name;
      input.value = `${name} (${code})`;
      hide();
      if (typeof opts.onSelect === 'function') opts.onSelect(code, name);
    }

    function getCurrentCode() {
      // 输入框文本是否仍是选中项（"名称 (代码)"）→ 有效选中
      const v = input.value.trim();
      if (state.code && v === `${state.name} (${state.code})`) return state.code;
      return '';
    }

    input.addEventListener('focus', () => { render(input.value); show(); });
    input.addEventListener('input', () => { render(input.value); show(); });
    input.addEventListener('keydown', (e) => {
      const els = [...list.querySelectorAll('.picker-item')];
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        const idx = els.findIndex(x => x.classList.contains('active'));
        const ni = Math.min(idx + 1, els.length - 1);
        els.forEach(x => x.classList.remove('active'));
        if (els[ni]) { els[ni].classList.add('active'); els[ni].scrollIntoView({ block: 'nearest' }); }
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        const idx = els.findIndex(x => x.classList.contains('active'));
        const ni = Math.max(idx - 1, 0);
        els.forEach(x => x.classList.remove('active'));
        if (els[ni]) { els[ni].classList.add('active'); els[ni].scrollIntoView({ block: 'nearest' }); }
      } else if (e.key === 'Enter') {
        const idx = els.findIndex(x => x.classList.contains('active'));
        if (idx >= 0) { e.preventDefault(); pick(els[idx]); }
      } else if (e.key === 'Escape') {
        hide();
      }
    });
    list.addEventListener('mousedown', (e) => {
      const item = e.target.closest('.picker-item');
      if (item) { e.preventDefault(); pick(item); }
    });
    document.addEventListener('click', (e) => {
      if (!container.contains(e.target)) hide();
    });

    render('');

    // 对外接口
    container._pickerGetValue = () => {
      const code = getCurrentCode();
      return code ? { code, name: state.name } : null;
    };
    container._pickerGetInput = () => input.value.trim();
    container._pickerSetValue = (code, name) => {
      state.code = code; state.name = name;
      input.value = `${name} (${code})`;
    };
  }

  window.StockPicker = {
    init: init,
    getValue: (el) => (el._pickerGetValue ? el._pickerGetValue() : null),
    getInput: (el) => (el._pickerGetInput ? el._pickerGetInput() : ''),
    setValue: (el, code, name) => (el._pickerSetValue ? el._pickerSetValue(code, name) : null)
  };
})();
