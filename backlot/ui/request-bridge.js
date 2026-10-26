(async () => {
  if (!('serviceWorker' in navigator)) throw new Error('此浏览器不支持安全会话协调');
  await navigator.serviceWorker.register('/auth/session-worker.js', { scope: '/' });
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) {
    await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, {once:true}));
  }
  const next = document.body.dataset.next || '/';
  location.replace(next.startsWith('/') && !next.startsWith('//') && !next.includes('\\') ? next : '/');
})().catch(() => { document.body.textContent = '应用会话恢复失败，请检查网络后刷新重试。'; });
