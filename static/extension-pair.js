'use strict';
const $ = id => document.getElementById(id);
let request = null;
// Comparison code only, never device_code/access_token. Fragments never reach logs.
const fragment = new URLSearchParams(location.hash.slice(1));
$('code').value = fragment.get('user_code') || fragment.get('code') || '';
history.replaceState(null, '', location.pathname);
async function post(action, body) {
  const response = await fetch('/api/auth/extension/' + action, {
    method: 'POST', credentials: 'same-origin', redirect: 'error',
    headers: {'Content-Type': 'application/json', 'X-Hermes-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
    body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
$('lookup').addEventListener('submit', async event => {
  event.preventDefault(); request = null; $('grant').hidden = true;
  try {
    request = await post('inspect', {user_code: $('code').value.trim().toUpperCase()});
    $('client').textContent = request.client_name;
    $('identity').textContent = request.extension_id;
    $('profile').textContent = request.profile;
    $('scopes').textContent = request.scopes.join(', ');
    $('power').textContent = [request.scopes.includes('control') ? 'CONTROL: settings, files, terminal, Git, memory, scheduling and other administrative actions, including shared Kanban boards across profiles.' : '', request.scopes.includes('cdp') ? 'CDP: inspect and control browser tabs through this device’s relay. This does not enable server CDP configuration.' : ''].filter(Boolean).join(' ');
    $('grant').hidden = false; $('status').textContent = '';
  } catch (error) { $('status').textContent = error.message; }
});
for (const action of ['approve', 'deny']) $(action).addEventListener('click', async () => {
  if (!request) return;
  $('approve').disabled = $('deny').disabled = true;
  try {
    await post(action, {user_code: request.user_code, profile: request.profile, scopes: request.scopes, confirm: true});
    $('grant').hidden = true; request = null;
    $('status').textContent = action === 'approve' ? 'Approved. Return to your extension.' : 'Request denied.';
  } catch (error) { $('status').textContent = error.message; }
  finally { $('approve').disabled = $('deny').disabled = false; }
});
