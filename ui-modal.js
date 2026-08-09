// ============ 全局错误/信息弹窗（替换页面下方静态提示） ============
// 用法:
//   showErrorModal(msg)  错误弹窗（红色标题，msg 为空时仅隐藏）
//   showInfoModal(msg)   信息弹窗（绿色标题）
//   hideErrorModal()     手动隐藏
(function () {
  let modalEl = null;

  // 隐藏弹窗（始终可用，弹窗未创建时静默跳过）
  window.hideErrorModal = function () {
    if (modalEl) modalEl.style.display = 'none';
  };

  function ensureModal() {
    if (modalEl) return modalEl;
    modalEl = document.createElement('div');
    modalEl.style.cssText =
      'position:fixed; inset:0; background:rgba(0,0,0,0.45); display:none;' +
      'align-items:center; justify-content:center; z-index:99999;';
    modalEl.innerHTML =
      '<div style="background:#fff; border-radius:8px; max-width:520px; width:92%;' +
      'box-shadow:0 8px 30px rgba(0,0,0,0.25); overflow:hidden;">' +
      '<div id="gmTitle" style="display:flex; justify-content:space-between; align-items:center;' +
      'padding:12px 18px; font-weight:600; font-size:15px;">' +
      '<span id="gmTitleText"></span>' +
      '<span id="gmClose" style="cursor:pointer; font-size:20px; line-height:1;">&times;</span>' +
      '</div>' +
      '<div id="gmBody" style="padding:18px; font-size:13px; color:#333; line-height:1.6;' +
      'max-height:60vh; overflow:auto; white-space:pre-wrap; word-break:break-all;"></div>' +
      '<div style="padding:12px 18px; text-align:right; border-top:1px solid #eee;">' +
      '<button id="gmOk" style="background:#1a73e8; color:#fff; border:none; padding:7px 26px;' +
      'border-radius:4px; cursor:pointer; font-size:13px;">知道了</button>' +
      '</div></div>';
    document.body.appendChild(modalEl);

    function hide() { modalEl.style.display = 'none'; }
    modalEl.addEventListener('click', (e) => { if (e.target === modalEl) hide(); });
    modalEl.querySelector('#gmClose').onclick = hide;
    modalEl.querySelector('#gmOk').onclick = hide;
    return modalEl;
  }

  function show(msg, title, color) {
    if (!msg) { window.hideErrorModal && window.hideErrorModal(); return; }
    const m = ensureModal();
    m.querySelector('#gmTitleText').textContent = title;
    m.querySelector('#gmTitle').style.background = color;
    m.querySelector('#gmTitle').style.color = '#fff';
    m.querySelector('#gmBody').textContent = String(msg);
    m.style.display = 'flex';
  }

  window.showErrorModal = (msg) => show(msg, '操作失败', '#d93025');
  window.showInfoModal = (msg) => show(msg, '提示', '#34a853');
})();
