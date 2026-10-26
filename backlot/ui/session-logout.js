// The browser must prove the actual OP suppression Cookie after logout-complete.
// RP state is retained until its server independently confirms that completion.
async function post(url, body, external = false) {
  const response = await fetch(url, {method:'POST',credentials:external?'include':'same-origin',redirect:'error',
    headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  if (!response.ok) throw new Error('Logout confirmation unavailable');
  const value = await response.json();
  return value.data || value;
}
export async function logoutFromUI(button) {
  button.disabled = true;
  document.body.replaceChildren(button);
  try {
    const status = await (await fetch(APP_BASE + '/auth/status',{cache:'no-store'})).json();
    const result = await post(APP_BASE + '/auth/logout', {scope:status.logoutScope || 'device'});
    if (result.session_context === 'browser') {
      await post(`${IDENTITY_ORIGIN}/auth/session/v2/logout-complete`, {logout_ticket:result.logout_ticket}, true);
      await post(`${IDENTITY_ORIGIN}/auth/session/v2/logout-cookie-confirm`, {logout_ticket:result.logout_ticket}, true);
      await post(APP_BASE + '/auth/logout-confirm', {});
    }
    location.assign(APP_BASE + '/auth');
    return;
  } catch {}
  button.textContent = '本机已退出，点击重试确认服务器退出';
  button.disabled = false;
}
export async function restoreLogout() {
  const response = await fetch(APP_BASE + '/auth/status',{cache:'no-store'});
  if (!response.ok || !(await response.json()).logoutPending) return;
  const login = document.querySelector('#login');
  if (login) login.hidden = true;
  const button = document.createElement('button');
  button.textContent = '继续完成退出登录';
  button.onclick = () => logoutFromUI(button);
  document.body.append(button);
}
