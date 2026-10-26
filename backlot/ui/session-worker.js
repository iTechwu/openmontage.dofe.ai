// One browser logical request retains its operation id through cookie confirmation.
// Neither AT nor RT is exposed to this worker or the page.
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
let cookieConfirmation = Promise.resolve();
async function confirm(ticket) {
  const renew = await fetch(`${IDENTITY_ORIGIN}/auth/session/renew-cookie`, {
    method: 'POST', credentials: 'include', redirect: 'error',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ ticket }),
  });
  if (!renew.ok) throw new Error('Central session renewal rejected');
  const body = await renew.json();
  const { challenge } = body.data || body;
  const response = await fetch(`${IDENTITY_ORIGIN}/auth/session/v2/confirm-cookie`, {
    method: 'POST', credentials: 'include', redirect: 'error',
    headers: { 'content-type': 'application/json' }, body: JSON.stringify({ challenge }),
  });
  if (!response.ok) throw new Error('Central session confirmation rejected');
}
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/auth') ||
      url.pathname.startsWith('/static/') || url.pathname.startsWith('/ui/') ||
      url.pathname.endsWith('/events') || url.pathname === '/api/health' || url.pathname === '/favicon.ico') return;
  event.respondWith((async () => {
    const headers = new Headers(event.request.headers);
    headers.set('x-dofe-operation-id', crypto.randomUUID());
    const original = new Request(event.request, { headers });
    // A 409 gate proves no business handler ran. Network failures and other
    // statuses are never replayed, especially for mutation/Range requests.
    for (let count = 0; count < 8; count++) {
      const response = await fetch(original.clone());
      if (response.status !== 409 || !response.headers.get('content-type')?.includes('application/json')) return response;
      const value = await response.clone().json();
      if (value.error !== 'COOKIE_RENEWAL_REQUIRED') return response;
      const current = cookieConfirmation.catch(() => {}).then(() => confirm(value.ticket));
      cookieConfirmation = current;
      try { await current; }
      catch { return new Response(JSON.stringify({error:'CENTRAL_SESSION_UNAVAILABLE'}), {status:503,headers:{'content-type':'application/json'}}); }
    }
    return new Response(JSON.stringify({error:'SESSION_BUSY'}), {status:503,headers:{'content-type':'application/json'}});
  })());
});
