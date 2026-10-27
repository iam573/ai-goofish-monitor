export function initDeviceScreen({getDevice, closeMenu}) {
  const dialog = document.querySelector('#device-screen');
  const image = document.querySelector('#device-screen-image');
  const status = document.querySelector('#device-screen-status');
  const time = document.querySelector('#device-screen-time');
  const refresh = document.querySelector('#device-screen-refresh');
  let device = null, controller = null, imageUrl = null, requestId = 0;
  function clearImage() {
    image.hidden = true;
    image.removeAttribute('src');
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = null;
  }
  async function capture() {
    if (!device || refresh.disabled) return;
    const id = ++requestId;
    controller = new AbortController();
    const current = controller;
    const timeout = setTimeout(() => current.abort('timeout'), 35000);
    refresh.disabled = true;
    status.classList.remove('error');
    status.textContent = imageUrl ? '正在刷新截图…' : '正在获取手机画面…';
    dialog.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(`/api/devices/${encodeURIComponent(device.id)}/screenshot`, {
        cache: 'no-store', signal: current.signal,
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(typeof data.detail === 'string' ? data.detail : '截图失败，请刷新重试');
      }
      const blob = await response.blob();
      if (!blob.type.startsWith('image/')) throw new Error('未取得有效截图，请刷新重试');
      if (id !== requestId || !dialog.open) return;
      clearImage();
      imageUrl = URL.createObjectURL(blob);
      image.src = imageUrl;
      image.hidden = false;
      const captured = response.headers.get('X-Captured-At');
      time.textContent = `${captured ? new Date(captured).toLocaleTimeString('zh-CN', {hour12: false}) : new Date().toLocaleTimeString('zh-CN', {hour12: false})} 拍摄`;
      status.textContent = '当前截图 · 点击刷新更新画面';
    } catch (error) {
      if (id !== requestId || !dialog.open) return;
      status.classList.add('error');
      status.textContent = current.signal.aborted ? '截图超时，请刷新重试' : error.message;
      if (imageUrl) status.textContent += '（下方保留上次截图）';
    } finally {
      clearTimeout(timeout);
      if (id === requestId) {
        refresh.disabled = false;
        dialog.setAttribute('aria-busy', 'false');
        controller = null;
      }
    }
  }
  document.querySelector('#device-options').addEventListener('click', event => {
    const button = event.target.closest('[data-device-screenshot]');
    if (!button || button.disabled) return;
    device = getDevice(button.closest('[data-device]').dataset.device);
    if (!device) return;
    closeMenu();
    clearImage();
    time.textContent = '';
    document.querySelector('#device-screen-title').textContent = `${device.name} · 当前画面`;
    image.alt = `${device.name} 的屏幕截图`;
    refresh.disabled = false;
    dialog.showModal();
    capture();
  });
  refresh.addEventListener('click', capture);
  document.querySelector('#device-screen-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  });
  dialog.addEventListener('close', () => {
    ++requestId;
    controller?.abort();
    controller = null;
    clearImage();
    device = null;
    refresh.disabled = false;
    dialog.setAttribute('aria-busy', 'false');
  });
}
