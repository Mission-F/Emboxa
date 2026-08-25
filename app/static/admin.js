const csrf = document.querySelector('meta[name="csrf-token"]').content;
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const bytes = value => {
  let number = Number(value || 0), index = 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index += 1; }
  return `${number.toFixed(index ? 1 : 0)} ${units[index]}`;
};

const preference = localStorage.getItem('emboxa-locale') || 'auto';
const language = $('#admin-language');
language.value = preference;
const t = key => window.EMBOXA_I18N?.t(key, language.value) || key;
const applyLanguage = value => {
  const locale = window.EMBOXA_I18N?.apply(value) || 'en';
  document.querySelectorAll('[data-i18n-placeholder]').forEach(node => {
    node.placeholder = window.EMBOXA_I18N.t(node.dataset.i18nPlaceholder, value);
  });
  return locale;
};
applyLanguage(preference);
language.addEventListener('change', () => { localStorage.setItem('emboxa-locale', language.value); applyLanguage(language.value); loadUsers(); loadSettings(); loadOperations(); });

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf);
  const response = await fetch(url, {...options, headers});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message;
    throw new Error(detail || 'The request could not be completed.');
  }
  return data;
}

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.className = 'toast'; }, 3500);
}

async function busy(button, label, task) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await task(); }
  finally { button.disabled = false; button.textContent = original; }
}

async function loadUsers() {
  const users = await api('/api/admin/users?q=' + encodeURIComponent($('#admin-search').value));
  $('#admin-users').innerHTML = users.map(user => `<article data-user="${user.id}">
    <div class="user-identity"><b>${esc(user.email)}</b><span>${user.verified ? t('verified') : t('unverified')} · ${esc(user.status)} · ${esc(user.role)}</span></div>
    <div><b>${esc(user.plan)}</b><span>${user.mailbox_count} ${t('mailboxes')} · ${bytes(user.storage_used)} / ${user.storage_limit == null ? t('unlimited') : bytes(user.storage_limit)}</span></div>
    <label><span>${t('plan')}</span><select data-field="plan"><option ${user.plan === 'STANDARD' ? 'selected' : ''}>STANDARD</option><option ${user.plan === 'PLUS' ? 'selected' : ''}>PLUS</option></select></label>
    <label><span>${t('status')}</span><select data-field="status"><option ${user.status === 'active' ? 'selected' : ''}>active</option><option ${user.status === 'suspended' ? 'selected' : ''}>suspended</option></select></label>
    <label><span>${t('quotaBytes')}</span><input data-field="quota" type="number" min="1" value="${user.storage_limit || 16106127360}"></label>
    <button class="secondary" type="button" data-save>${t('save')}</button>
  </article>`).join('') || `<p class="empty-admin">${t('noUsers')}</p>`;
}

$('#admin-users').addEventListener('click', async event => {
  const button = event.target.closest('[data-save]');
  if (!button) return;
  const row = button.closest('[data-user]');
  const body = {
    plan: row.querySelector('[data-field=plan]').value,
    status: row.querySelector('[data-field=status]').value,
    storage_limit_bytes: Number(row.querySelector('[data-field=quota]').value),
  };
  try {
    await busy(button, 'Saving…', () => api(`/api/admin/users/${row.dataset.user}`, {method: 'PATCH', body: JSON.stringify(body)}));
    toast(t('userUpdated'));
    await loadUsers();
  } catch (error) {
    if (error.message.includes('confirmation') && confirm(`${error.message}\nProceed without deleting data?`)) {
      body.confirm_downgrade = true;
      await api(`/api/admin/users/${row.dataset.user}`, {method: 'PATCH', body: JSON.stringify(body)});
      toast(t('userUpdated'));
      await loadUsers();
    } else toast(error.message, true);
  }
});

const form = $('#admin-settings-form');
const numericFields = new Set(['smtp_port','standard_storage_limit_bytes','standard_mailbox_limit','standard_retention_days','permanent_mailbox_limit','permanent_mailbox_lock_days','backup_concurrency','default_backup_retention_versions','backup_anomaly_threshold','export_ttl_hours','export_max_bytes']);
const checkboxFields = new Set(['smtp_enabled','registration_enabled','backup_queue_enabled','cleanup_enabled','analytics_enabled']);
const webhookLabel = status => ({connected: t('webhookActive'), failed: t('webhookFailed'), warning: t('webhookWarning'), not_configured: t('webhookNotConfigured'), unknown: t('webhookUnknown')})[status] || status;

async function loadSettings() {
  const data = await api('/api/admin/settings');
  for (const element of form.elements) {
    if (!element.name || !(element.name in data)) continue;
    if (element.type === 'checkbox') element.checked = Boolean(data[element.name]);
    else if (!element.dataset.secret) element.value = data[element.name] ?? '';
  }
  const smtpMask = data.smtp_password_masked || '';
  const telegramMask = data.telegram_bot_token_masked || '';
  form.smtp_password.placeholder = smtpMask || t('enterNewPassword');
  form.telegram_bot_token.placeholder = telegramMask || t('enterNewToken');
  $('#smtp-password-state').textContent = smtpMask ? t('passwordEncrypted') : t('notConfigured');
  $('#telegram-token-state').textContent = telegramMask ? t('tokenEncrypted') : t('notConfigured');
  $('#telegram-test-status').textContent = data.telegram_connected ? t('configured') : t('notConfigured');
  $('#telegram-bot-identity').textContent = data.telegram_bot_username ? `@${data.telegram_bot_username}` : '—';
  $('#telegram-webhook-state').textContent = webhookLabel(data.telegram_webhook_status);
  $('#telegram-webhook-detail').textContent = data.telegram_webhook_error || (data.telegram_webhook_status === 'connected' ? data.telegram_webhook_url : t('webhookHelp'));
  $('#telegram-disconnect').disabled = !data.telegram_bot_token_set;
  $('#telegram-webhook-retry').disabled = !data.telegram_bot_token_set;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = form.querySelector('[type=submit]');
  const body = Object.fromEntries(new FormData(form));
  checkboxFields.forEach(name => { body[name] = form.elements[name].checked; });
  numericFields.forEach(name => { body[name] = Number(body[name]); });
  body.smtp_password = form.smtp_password.value;
  body.telegram_bot_token = form.telegram_bot_token.value;
  const status = $('#settings-status');
  status.textContent = '';
  try {
    const result = await busy(button, 'Saving…', () => api('/api/admin/settings', {method: 'PUT', body: JSON.stringify(body)}));
    form.smtp_password.value = '';
    form.telegram_bot_token.value = '';
    await loadSettings();
    const warning = result.telegram?.warning || '';
    status.textContent = warning || t('settingsSaved');
    status.className = `inline-status ${warning ? 'error' : 'success'}`;
    toast(warning || t('configurationSaved'), Boolean(warning));
  } catch (error) {
    status.textContent = error.message;
    status.className = 'inline-status error';
  }
});

document.querySelectorAll('[data-toggle-secret]').forEach(button => button.addEventListener('click', () => {
  const input = button.parentElement.querySelector('input');
  input.type = input.type === 'password' ? 'text' : 'password';
  button.textContent = t(input.type === 'password' ? 'show' : 'hide');
  input.focus();
}));

$('#smtp-test').addEventListener('click', async () => {
  const button = $('#smtp-test'), status = $('#smtp-test-status');
  status.textContent = '';
  try {
    const email = $('#smtp-test-email').value.trim();
    const result = await busy(button, 'Testing…', () => api('/api/admin/smtp/test', {method: 'POST', body: JSON.stringify({email: email || null})}));
    status.textContent = result.message;
    status.className = 'inline-status success';
  } catch (error) { status.textContent = error.message; status.className = 'inline-status error'; }
});

async function connectTelegram(button = $('#telegram-connect')) {
  const token = form.telegram_bot_token.value.trim();
  if (!token) throw new Error(t('pasteTokenFirst'));
  const result = await busy(button, t('connecting'), () => api('/api/admin/telegram/connect', {method: 'POST', body: JSON.stringify({token})}));
  form.telegram_bot_token.value = '';
  await loadSettings();
  return result;
}

$('#telegram-connect').addEventListener('click', async () => {
  const status = $('#telegram-test-status');
  try {
    const result = await connectTelegram();
    status.textContent = result.warning || `${t('botConnected')}${result.username ? ` · @${result.username}` : ''}`;
    status.className = `inline-status ${result.warning ? 'error' : 'success'}`;
    toast(status.textContent, Boolean(result.warning));
  } catch (error) { status.textContent = error.message; status.className = 'inline-status error'; }
});

$('#telegram-test').addEventListener('click', async () => {
  const button = $('#telegram-test'), status = $('#telegram-test-status');
  try {
    if (form.telegram_bot_token.value.trim()) await connectTelegram(button);
    const result = await busy(button, 'Testing…', () => api('/api/admin/telegram/test', {method: 'POST'}));
    const webhook = ` · ${webhookLabel(result.webhook_status)}`;
    status.textContent = `${result.message}${result.username ? ` · @${result.username}` : ''}${webhook}`;
    status.className = `inline-status ${result.webhook_status === 'connected' ? 'success' : 'error'}`;
    await loadSettings();
  } catch (error) { status.textContent = error.message; status.className = 'inline-status error'; }
});

$('#telegram-webhook-retry').addEventListener('click', async () => {
  const button = $('#telegram-webhook-retry'), status = $('#telegram-test-status');
  try {
    const result = await busy(button, 'Connecting…', () => api('/api/admin/telegram/webhook', {method: 'POST'}));
    status.textContent = result.message;
    status.className = 'inline-status success';
    await loadSettings();
  } catch (error) { status.textContent = error.message; status.className = 'inline-status error'; await loadSettings(); }
});

$('#telegram-disconnect').addEventListener('click', async () => {
  const button = $('#telegram-disconnect');
  try {
    await busy(button, 'Disconnecting…', () => api('/api/admin/telegram', {method: 'DELETE'}));
    form.telegram_bot_token.value = '';
    await loadSettings();
    toast(t('botDisconnected'));
  } catch (error) { toast(error.message, true); }
});

async function loadOperations() {
  const data = await api('/api/admin/operations');
  $('#admin-stats').innerHTML = `<article><b>${data.users}</b><span>${t('totalUsers')}</span></article><article><b>${bytes(data.storage_total)}</b><span>${t('archives')}</span></article><article><b>${data.backup_queue_status.running}</b><span>${t('runningBackups')}</span></article><article><b>${data.backup_queue_status.queued}</b><span>${t('queuedBackups')}</span></article>`;
  const statuses = [[t('appVersion'), data.app_version], [t('database'), data.database_status], [t('worker'), data.worker_status], [t('backupQueue'), `${data.backup_queue_status.running} ${t('running')} · ${data.backup_queue_status.queued} ${t('queued')}`], [t('storageUsed'), `${bytes(data.storage_used)} / ${bytes(data.storage_capacity)}`], [t('lastCleanup'), data.last_cleanup || t('notRunYet')]];
  $('#maintenance-status').innerHTML = statuses.map(([label, value]) => `<article><span>${esc(label)}</span><b>${esc(value)}</b></article>`).join('');
  $('#admin-jobs').innerHTML = data.jobs.map(job => `<article><div><b>#${job.id} · ${esc(job.status)}</b><span>Mailbox ${job.account_id}${job.error ? ` · ${esc(job.error)}` : ''}</span></div></article>`).join('') || `<p class="empty-admin">${t('noOperations')}</p>`;
}

$('#run-cleanup').addEventListener('click', async () => {
  const button = $('#run-cleanup');
  try {
    const result = await busy(button, 'Cleaning…', () => api('/api/admin/maintenance/cleanup', {method: 'POST'}));
    toast(result.message);
    await loadOperations();
  } catch (error) { toast(error.message, true); }
});

let searchTimer;
$('#admin-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadUsers, 250); });
Promise.all([loadUsers(), loadSettings(), loadOperations()]).catch(error => toast(error.message, true));
