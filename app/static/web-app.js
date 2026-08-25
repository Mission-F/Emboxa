async function loadWebUsage() {
  try {
    const usage = await api('/api/web/usage');
    const limit = usage.storage_limit == null ? 'Unlimited' : bytes(usage.storage_limit);
    const mailLimit = usage.mailbox_limit == null ? 'Unlimited' : usage.mailbox_limit;
    const percent = usage.storage_limit ? Math.min(100, usage.storage_used * 100 / usage.storage_limit) : 0;
    const quota = usage.imap_transfer_quota;
    const restoreLimit = quota.limit == null ? 'Unlimited' : `${quota.used} / ${quota.limit}`;
    document.querySelector('#web-usage').innerHTML = `<div class="usage-plan"><b>${esc(usage.plan)}</b><span>Plan</span></div><div class="usage-storage"><b>${bytes(usage.storage_used)} / ${limit}</b><span>Storage used</span>${usage.storage_limit ? `<i><em style="width:${percent}%"></em></i>` : ''}</div><div><b>${usage.mailbox_count} / ${mailLimit}</b><span>Mailboxes</span></div><div><b>${usage.active_backups}</b><span>Active backups</span></div><div><b>${usage.active_transfers}</b><span>Active restores</span></div><div><b>${restoreLimit}</b><span>Mailbox restores this month</span></div>${usage.over_quota ? '<strong class="over-quota">Over quota</strong>' : ''}`;
  } catch (_) { console.warn('Usage unavailable'); }
}

const telegramPrefs = document.createElement('div');
telegramPrefs.className = 'telegram-preferences';
telegramPrefs.innerHTML = '<b>Notifications</b><label><input type="checkbox" data-pref="notify_completed"> Backup or restore completed</label><label><input type="checkbox" data-pref="notify_failed"> Backup or restore failed</label><label><input type="checkbox" data-pref="notify_expiring"> Backup expiring</label><label><input type="checkbox" data-pref="notify_storage"> Storage almost full</label>';
document.querySelector('.settings-body hr').after(telegramPrefs);

async function loadTelegram() {
  try {
    const item = await api('/api/telegram/link');
    document.querySelector('#telegram-chat-id').value = item.chat_id || '';
    const status = document.querySelector('#telegram-user-status'), open = document.querySelector('#telegram-open-bot');
    if (item.bot_username) { open.href = `https://t.me/${encodeURIComponent(item.bot_username)}`; open.classList.remove('hidden'); } else open.classList.add('hidden');
    status.textContent = !item.bot_configured ? 'Telegram notifications are not configured by the administrator yet.' : item.linked ? `Your account is linked${item.bot_username ? ` to @${item.bot_username}` : ''}.` : `The bot is ready${item.bot_username ? ` (@${item.bot_username})` : ''}. Open it, send /start, then paste the Chat ID it shows here.`;
    for (const input of telegramPrefs.querySelectorAll('[data-pref]')) input.checked = Boolean(item.preferences?.[input.dataset.pref]);
  } catch (_) {}
}
document.querySelector('#telegram-connect').addEventListener('click', async () => { try { await api('/api/telegram/link',{method:'PUT',body:JSON.stringify({chat_id:document.querySelector('#telegram-chat-id').value.trim()})}); await loadTelegram(); toast('Your Telegram Chat ID is linked'); } catch(error){ toast(error.message,'error'); } });
document.querySelector('#telegram-test').addEventListener('click', async () => { try { await api('/api/telegram/test',{method:'POST'}); toast('Telegram dashboard updated'); } catch(error){ toast(error.message,'error'); } });
document.querySelector('#telegram-disconnect').addEventListener('click', async () => { try { await api('/api/telegram/link',{method:'DELETE'}); document.querySelector('#telegram-chat-id').value=''; toast('Telegram disconnected'); } catch(error){ toast(error.message,'error'); } });
telegramPrefs.addEventListener('change', async () => { const body=Object.fromEntries([...telegramPrefs.querySelectorAll('[data-pref]')].map(input=>[input.dataset.pref,input.checked])); try { await api('/api/telegram/preferences',{method:'PATCH',body:JSON.stringify(body)}); } catch(error){ toast(error.message,'error'); } });

const transferDialog = document.querySelector('#transfer-dialog');
const transferForm = document.querySelector('#transfer-form');
const transferStatus = document.querySelector('#transfer-status');
let transferStep = 1, transferPreviewData = null;
const providerSettings = {
  gmail:{host:'imap.gmail.com',port:993,security:'ssl'}, outlook:{host:'outlook.office365.com',port:993,security:'ssl'},
  yahoo:{host:'imap.mail.yahoo.com',port:993,security:'ssl'}, icloud:{host:'imap.mail.me.com',port:993,security:'ssl'}, custom:{host:'',port:993,security:'ssl'}
};

function transferDestination() {
  const values = Object.fromEntries(new FormData(transferForm));
  if (values.destination_kind === 'existing') return {account_id:Number(values.destination_account_id)};
  return {label:values.label,imap_host:values.imap_host,imap_port:Number(values.imap_port),security:values.security,imap_username:values.imap_username||values.label,password:values.password};
}
function showTransferStep(step) {
  transferStep = Math.max(1,Math.min(4,step));
  document.querySelectorAll('[data-transfer-step]').forEach(node=>node.classList.toggle('hidden',Number(node.dataset.transferStep)!==transferStep));
  document.querySelectorAll('[data-step-marker]').forEach(node=>node.classList.toggle('active',Number(node.dataset.stepMarker)<=transferStep));
  document.querySelector('#transfer-back').classList.toggle('hidden',transferStep===1);
  document.querySelector('#transfer-next').classList.toggle('hidden',transferStep===4);
  document.querySelector('#transfer-start').classList.toggle('hidden',transferStep!==4);
  if (transferStep===4) { renderTransferReview(); loadTransferJobs(); }
}
async function loadTransferSource() {
  const accountId = transferForm.elements.account_id.value;
  if (!accountId) return;
  try {
    const versions = await api(`/api/accounts/${accountId}/versions`);
    const versionSelect = transferForm.elements.snapshot_id;
    versionSelect.innerHTML = versions.map(item=>`<option value="${item.id}" ${item.current?'selected':''}>${item.current?'Current · ':''}${new Date(item.completed_at||item.created_at).toLocaleString()} · ${item.message_count.toLocaleString()} messages</option>`).join('');
    await loadTransferPreview();
  } catch(error){ transferStatus.textContent=error.message; }
}
async function loadTransferPreview() {
  const accountId=transferForm.elements.account_id.value, snapshotId=transferForm.elements.snapshot_id.value;
  if (!accountId) return;
  try {
    transferPreviewData=await api(`/api/accounts/${accountId}/transfer-preview${snapshotId?`?snapshot_id=${snapshotId}`:''}`);
    const q=transferPreviewData.quota, quota=q.limit==null?'PLUS · unlimited restores':`STANDARD · ${q.remaining} of ${q.limit} restores remaining this month`;
    document.querySelector('#transfer-preview').innerHTML=`<b>${transferPreviewData.snapshot.messages.toLocaleString()} messages · ${bytes(transferPreviewData.snapshot.size)}</b><span>${transferPreviewData.folders.length} folders</span><small>${esc(quota)}</small>`;
  } catch(error){ transferStatus.textContent=error.message; }
}
function renderTransferReview() {
  const values=Object.fromEntries(new FormData(transferForm));
  const dest=values.destination_kind==='existing' ? transferForm.elements.destination_account_id.selectedOptions[0]?.textContent : values.label;
  const folders=values.mode==='preserve'?'Preserve source folders':`One folder · ${values.single_folder}`;
  document.querySelector('#transfer-review').innerHTML=`<div><span>Archive</span><b>${esc(transferForm.elements.account_id.selectedOptions[0]?.textContent)}</b></div><div><span>Content</span><b>${transferPreviewData?.snapshot.messages.toLocaleString()||'—'} messages · ${bytes(transferPreviewData?.snapshot.size||0)}</b></div><div><span>Destination</span><b>${esc(dest||'Not selected')}</b></div><div><span>Folders</span><b>${esc(folders)}</b></div><small>The monthly quota is used only when this restore is queued.</small>`;
}
async function openTransfer(preferredAccountId=null) {
  const accounts=await api('/api/accounts');
  const sources=accounts.filter(item=>item.has_archive), source=transferForm.elements.account_id, destination=transferForm.elements.destination_account_id;
  source.innerHTML=sources.map(item=>`<option value="${item.id}">${esc(item.display_name)} · ${esc(item.email)}</option>`).join('');
  destination.innerHTML=accounts.map(item=>`<option value="${item.id}">${esc(item.display_name)} · ${esc(item.email)}</option>`).join('');
  if (preferredAccountId && [...source.options].some(option=>Number(option.value)===Number(preferredAccountId))) source.value=String(preferredAccountId);
  if (!source.value){ toast('Create a backup before restoring to a mailbox','error'); return; }
  transferStatus.textContent=''; showTransferStep(1); transferDialog.showModal(); await loadTransferSource();
}
async function loadTransferJobs() {
  try {
    const data=await api('/api/imap-transfers'), active=data.items.filter(item=>['queued','running','cancelling'].includes(item.status)), history=data.items.filter(item=>!['queued','running','cancelling'].includes(item.status)).slice(0,5);
    document.querySelector('#transfer-jobs').innerHTML=active.map(item=>`<article><div><b>Restore #${item.id} · ${esc(item.destination_label)}</b><span>${esc(item.current_folder||item.status)} · ${item.processed_messages}/${item.total_messages} · ${item.percent}%${item.eta_seconds!=null?` · ETA ${duration(item.eta_seconds)}`:''}</span></div><div class="progress"><i style="width:${item.percent}%"></i></div><button class="text-button danger-text" type="button" data-cancel-transfer="${item.id}">Cancel restore</button></article>`).join('') || '<p class="muted">No restore is currently running.</p>';
    document.querySelector('#transfer-history').innerHTML=history.length?`<h4>Recent restores</h4>${history.map(item=>`<div><b>${esc(item.destination_label)}</b><span>${esc(item.status)} · ${item.processed_messages}/${item.total_messages}${item.error?` · ${esc(item.error)}`:''}</span></div>`).join('')}`:'';
  } catch(_){}
}

transferForm.elements.account_id.addEventListener('change',loadTransferSource);
transferForm.elements.snapshot_id.addEventListener('change',loadTransferPreview);
transferForm.addEventListener('change',event=>{
  if(event.target.name==='destination_kind'){ const isNew=event.target.value==='new'; document.querySelector('#transfer-existing').classList.toggle('hidden',isNew); document.querySelector('#transfer-temporary').classList.toggle('hidden',!isNew); }
  if(event.target.name==='mode') document.querySelector('#transfer-single-folder').classList.toggle('hidden',event.target.value!=='single');
});
transferForm.querySelector('[name="label"]').addEventListener('input',event=>{ const username=transferForm.querySelector('[name="imap_username"]'); if(!username.value||username.dataset.auto==='true'){username.value=event.target.value;username.dataset.auto='true';} });
transferForm.querySelector('[name="imap_username"]').addEventListener('input',event=>{event.target.dataset.auto='false';});
document.querySelectorAll('[data-transfer-provider]').forEach(button=>button.addEventListener('click',()=>{ const p=providerSettings[button.dataset.transferProvider]; transferForm.elements.imap_host.value=p.host; transferForm.elements.imap_port.value=p.port; transferForm.elements.security.value=p.security; document.querySelectorAll('[data-transfer-provider]').forEach(item=>item.classList.toggle('active',item===button)); }));
document.querySelector('#transfer-next').addEventListener('click',async()=>{ transferStatus.textContent=''; if(transferStep===1&&!transferPreviewData){transferStatus.textContent='Choose a valid archive version.';return;} if(transferStep===2){const values=Object.fromEntries(new FormData(transferForm));if(values.destination_kind==='existing'&&!values.destination_account_id){transferStatus.textContent='Choose a destination mailbox.';return;}if(values.destination_kind==='new'&&(!values.label||!values.password||!values.imap_host)){transferStatus.textContent='Choose a provider and enter the mailbox email and app password.';return;}} showTransferStep(transferStep+1); });
document.querySelector('#transfer-back').addEventListener('click',()=>showTransferStep(transferStep-1));
document.querySelector('#nav-transfers').addEventListener('click',()=>openTransfer());
document.querySelector('#transfer-test').addEventListener('click',async()=>{ const result=document.querySelector('#transfer-test-result'); result.textContent='Testing connection…'; try{const data=await api('/api/imap-transfer/test',{method:'POST',body:JSON.stringify({destination:transferDestination()})});result.textContent=`Connected · ${data.folders} folders · no quota used`;result.className='success';}catch(error){result.textContent=error.message;result.className='danger-text';} });
document.querySelector('#transfer-jobs').addEventListener('click',async event=>{const button=event.target.closest('[data-cancel-transfer]');if(!button)return;try{await api(`/api/imap-transfers/${button.dataset.cancelTransfer}/cancel`,{method:'POST'});toast('Restore cancellation requested');await Promise.all([loadTransferJobs(),loadWebUsage()]);}catch(error){toast(error.message,'error');}});
transferForm.addEventListener('submit',async event=>{event.preventDefault();const values=Object.fromEntries(new FormData(transferForm));transferStatus.textContent='Validating and queueing restore…';try{const result=await api(`/api/accounts/${values.account_id}/transfers`,{method:'POST',body:JSON.stringify({snapshot_id:Number(values.snapshot_id),destination:transferDestination(),mode:values.mode,single_folder:values.mode==='single'?values.single_folder:null,mappings:{},skip_duplicates:Boolean(values.skip_duplicates)})});toast(`Restore #${result.job.id} queued`);showTransferStep(4);await Promise.all([loadTransferJobs(),loadWebUsage()]);}catch(error){transferStatus.textContent=error.message;}});

document.querySelector('#nav-settings').addEventListener('click',loadTelegram);
loadWebUsage(); setInterval(()=>{loadWebUsage();if(transferDialog.open&&transferStep===4)loadTransferJobs();},15000);
