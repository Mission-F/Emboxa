async function loadWebUsage() {
  try {
    const usage = await api('/api/web/usage');
    const limit = usage.storage_limit == null ? 'Unlimited' : bytes(usage.storage_limit);
    const mailLimit = usage.mailbox_limit == null ? 'Unlimited' : usage.mailbox_limit;
    const percent = usage.storage_limit ? Math.min(100, usage.storage_used * 100 / usage.storage_limit) : 0;
    const quota = usage.imap_transfer_quota;
    const transferLimit = quota.limit == null ? 'Unlimited' : `${quota.used} / ${quota.limit}`;
    document.querySelector('#web-usage').innerHTML = `
      <div class="usage-plan"><b>${esc(usage.plan)}</b><span>Plan</span></div>
      <div class="usage-storage"><b>${bytes(usage.storage_used)} / ${limit}</b><span>Storage used</span>${usage.storage_limit ? `<i><em style="width:${percent}%"></em></i>` : ''}</div>
      <div><b>${usage.mailbox_count} / ${mailLimit}</b><span>Mailboxes</span></div>
      <div><b>${usage.active_backups}</b><span>Active backups</span></div>
      <div><b>${usage.active_transfers}</b><span>Active transfers</span></div>
      <div><b>${transferLimit}</b><span>IMAP transfers this month</span></div>
      ${usage.over_quota ? '<strong class="over-quota">Over quota</strong>' : ''}`;
  } catch (error) { console.warn('Usage unavailable'); }
}

const telegramPrefs = document.createElement('div');
telegramPrefs.className = 'telegram-preferences';
telegramPrefs.innerHTML = '<b>Notifications</b><label><input type="checkbox" data-pref="notify_completed"> Backup and transfer completed</label><label><input type="checkbox" data-pref="notify_failed"> Backup and transfer failed</label><label><input type="checkbox" data-pref="notify_expiring"> Backup expiring</label><label><input type="checkbox" data-pref="notify_storage"> Storage almost full</label>';
document.querySelector('.settings-body hr').after(telegramPrefs);

async function loadTelegram() {
  try {
    const item = await api('/api/telegram/link');
    document.querySelector('#telegram-chat-id').value = item.chat_id || '';
    const status = document.querySelector('#telegram-user-status');
    const open = document.querySelector('#telegram-open-bot');
    if (item.bot_username) { open.href = `https://t.me/${encodeURIComponent(item.bot_username)}`; open.classList.remove('hidden'); }
    else open.classList.add('hidden');
    status.textContent = !item.bot_configured ? 'Telegram notifications are not configured by the administrator yet.' : item.linked ? `Your account is linked${item.bot_username ? ` to @${item.bot_username}` : ''}.` : `The bot is ready${item.bot_username ? ` (@${item.bot_username})` : ''}. Open it, send /start, then paste the Chat ID it shows here.`;
    for (const input of telegramPrefs.querySelectorAll('[data-pref]')) input.checked = Boolean(item.preferences?.[input.dataset.pref]);
  } catch (_) {}
}

document.querySelector('#telegram-connect').addEventListener('click', async () => {
  try { await api('/api/telegram/link', {method:'PUT', body:JSON.stringify({chat_id:document.querySelector('#telegram-chat-id').value.trim()})}); await loadTelegram(); toast('Your Telegram Chat ID is linked'); }
  catch (error) { toast(error.message, 'error'); }
});
document.querySelector('#telegram-test').addEventListener('click', async () => {
  try { await api('/api/telegram/test', {method:'POST'}); toast('Telegram dashboard updated'); }
  catch (error) { toast(error.message, 'error'); }
});
document.querySelector('#telegram-disconnect').addEventListener('click', async () => {
  try { await api('/api/telegram/link', {method:'DELETE'}); document.querySelector('#telegram-chat-id').value = ''; toast('Telegram disconnected'); }
  catch (error) { toast(error.message, 'error'); }
});
telegramPrefs.addEventListener('change', async () => {
  const body = Object.fromEntries([...telegramPrefs.querySelectorAll('[data-pref]')].map(input => [input.dataset.pref, input.checked]));
  try { await api('/api/telegram/preferences', {method:'PATCH', body:JSON.stringify(body)}); }
  catch (error) { toast(error.message, 'error'); }
});

const transferDialog = document.querySelector('#transfer-dialog');
const transferForm = document.querySelector('#transfer-form');
const transferStatus = document.querySelector('#transfer-status');

function transferDestination() {
  const values = Object.fromEntries(new FormData(transferForm));
  const id = Number(values.destination_account_id || 0);
  return id ? {account_id:id} : {label:values.label, imap_host:values.imap_host, imap_port:Number(values.imap_port), security:values.security, imap_username:values.imap_username, password:values.password};
}

async function openTransfer(preferredAccountId = null) {
  const accounts = await api('/api/accounts');
  const source = transferForm.elements.account_id;
  const destination = transferForm.elements.destination_account_id;
  source.innerHTML = accounts.filter(item => item.has_archive).map(item => `<option value="${item.id}">${esc(item.display_name)} · ${esc(item.email)}</option>`).join('');
  destination.innerHTML = '<option value="">Temporary IMAP credentials</option>' + accounts.map(item => `<option value="${item.id}">${esc(item.display_name)} · ${esc(item.email)}</option>`).join('');
  transferStatus.textContent = '';
  if (preferredAccountId && [...source.options].some(option => Number(option.value) === Number(preferredAccountId))) source.value = String(preferredAccountId);
  if (!source.value) { toast('Create a backup before starting an IMAP transfer', 'error'); return; }
  transferDialog.showModal();
  await Promise.all([loadTransferPreview(), loadTransferJobs()]);
}

async function loadTransferJobs() {
  try {
    const data = await api('/api/imap-transfers');
    const active = data.items.filter(item => ['queued','running','cancelling'].includes(item.status));
    document.querySelector('#transfer-jobs').innerHTML = active.map(item => `<article><div><b>#${item.id} · ${esc(item.destination_label)}</b><span>${esc(item.current_folder || item.status)} · ${item.processed_messages} / ${item.total_messages} · ${item.percent}%${item.eta_seconds != null ? ` · ETA ${duration(item.eta_seconds)}` : ''}</span></div><div class="progress"><i style="width:${item.percent}%"></i></div><button class="text-button danger-text" type="button" data-cancel-transfer="${item.id}">Cancel</button></article>`).join('');
  } catch (_) {}
}

document.querySelector('#transfer-jobs').addEventListener('click', async event => {
  const button = event.target.closest('[data-cancel-transfer]');
  if (!button) return;
  try { await api(`/api/imap-transfers/${button.dataset.cancelTransfer}/cancel`, {method:'POST'}); toast('Transfer cancellation requested'); await Promise.all([loadTransferJobs(), loadWebUsage()]); }
  catch (error) { toast(error.message, 'error'); }
});

async function loadTransferPreview() {
  if (!transferForm.elements.account_id.value) return;
  try {
    const preview = await api(`/api/accounts/${transferForm.elements.account_id.value}/transfer-preview`);
    const quota = preview.quota.limit == null ? 'PLUS · unlimited transfers' : `STANDARD · ${preview.quota.remaining} of ${preview.quota.limit} transfers remaining this month`;
    document.querySelector('#transfer-preview').innerHTML = `<b>${preview.snapshot.messages.toLocaleString()} messages · ${bytes(preview.snapshot.size)}</b><span>${preview.folders.length} folders</span><small>${esc(quota)}</small>`;
  } catch (error) { transferStatus.textContent = error.message; }
}

transferForm.elements.account_id.addEventListener('change', loadTransferPreview);
transferForm.elements.destination_account_id.addEventListener('change', event => document.querySelector('#transfer-temporary').classList.toggle('hidden', Boolean(event.target.value)));
transferForm.elements.mode.addEventListener('change', event => document.querySelector('#transfer-single-folder').classList.toggle('hidden', event.target.value !== 'single'));
document.querySelector('#nav-transfers').addEventListener('click', openTransfer);
document.querySelector('#transfer-test').addEventListener('click', async () => {
  transferStatus.textContent = 'Testing destination…';
  try { const result = await api('/api/imap-transfer/test', {method:'POST', body:JSON.stringify({destination:transferDestination()})}); transferStatus.textContent = `Connected · ${result.folders} folders · quota not consumed`; transferStatus.classList.add('success'); }
  catch (error) { transferStatus.textContent = error.message; transferStatus.classList.remove('success'); }
});
transferForm.addEventListener('submit', async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(transferForm));
  if (!confirm(`Start IMAP transfer?\n\n${document.querySelector('#transfer-preview').textContent.trim()}\n\nThe monthly quota is consumed when this job is queued.`)) return;
  transferStatus.textContent = 'Validating and queueing…';
  try {
    const result = await api(`/api/accounts/${values.account_id}/transfers`, {method:'POST', body:JSON.stringify({destination:transferDestination(), mode:values.mode, single_folder:values.mode === 'single' ? values.single_folder : null, mappings:{}, skip_duplicates:Boolean(values.skip_duplicates)})});
    transferDialog.close(); toast(`IMAP transfer #${result.job.id} queued`); await loadWebUsage();
  } catch (error) { transferStatus.textContent = error.message; }
});

document.querySelector('#nav-settings').addEventListener('click', loadTelegram);
loadWebUsage();
setInterval(loadWebUsage, 15000);
