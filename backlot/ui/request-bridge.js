(async () => {
  if (!('serviceWorker' in navigator)) throw new Error('此浏览器不支持安全会话协调');
  await navigator.serviceWorker.register(APP_BASE + '/auth/session-worker.js', { scope: APP_BASE + '/' });
  await navigator.serviceWorker.ready;
  const workerURL = new URL(APP_BASE + '/auth/session-worker.js', location.origin).href;
  if (navigator.serviceWorker.controller?.scriptURL !== workerURL) {
    await new Promise(resolve => {
      const changed = () => {
        if (navigator.serviceWorker.controller?.scriptURL !== workerURL) return;
        navigator.serviceWorker.removeEventListener('controllerchange', changed);
        resolve();
      };
      navigator.serviceWorker.addEventListener('controllerchange', changed);
      changed();
    });
  }
  const next = document.body.dataset.next || APP_BASE + '/';
  location.replace(next.startsWith(APP_BASE + '/') && !next.startsWith('//') && !next.includes('\\') ? next : APP_BASE + '/');
})().catch(() => { document.body.textContent = '应用会话恢复失败，请检查网络后刷新重试。'; });
