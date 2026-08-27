async function loadWebUsage() {
  try {
    const usage = await api('/api/web/usage');
    const limit = usage.storage_limit == null ? 'Illimitato' : bytes(usage.storage_limit);
    const mailLimit = usage.mailbox_limit == null ? 'Illimitate' : usage.mailbox_limit;
    const percent = usage.storage_limit ? Math.min(100, usage.storage_used * 100 / usage.storage_limit) : 0;
    const quota = usage.imap_transfer_quota;
    const restoreLimit = quota.limit == null ? 'Illimitati' : `${quota.used} / ${quota.limit}`;
    document.querySelector('#web-usage').innerHTML = `<div class="usage-plan"><b>${esc(usage.plan)}</b><span>Piano</span></div><div class="usage-storage"><b>${bytes(usage.storage_used)} / ${limit}</b><span>Spazio usato</span>${usage.storage_limit ? `<i><em data-progress="${percent}"></em></i>` : ''}</div><div><b>${usage.mailbox_count} / ${mailLimit}</b><span>Caselle</span></div><div><b>${usage.active_backups}</b><span>Backup attivi</span></div><div><b>${usage.active_transfers}</b><span>Ripristini attivi</span></div><div><b>${restoreLimit}</b><span>Ripristini questo mese</span></div>${usage.over_quota ? '<strong class="over-quota">Spazio esaurito</strong>' : ''}`;
    applyProgressWidths(document.querySelector('#web-usage'));
  } catch (_) { console.warn('Utilizzo non disponibile'); }
}

const telegramPrefs = document.createElement('div');
telegramPrefs.className = 'telegram-preferences';
telegramPrefs.innerHTML = '<b>Notifiche</b><label><input type="checkbox" data-pref="notify_completed"> Backup o ripristino completato</label><label><input type="checkbox" data-pref="notify_failed"> Backup o ripristino non riuscito</label><label><input type="checkbox" data-pref="notify_expiring"> Backup in scadenza</label><label><input type="checkbox" data-pref="notify_storage"> Spazio quasi esaurito</label>';
document.querySelector('.settings-body hr').after(telegramPrefs);

async function loadTelegram() {
  try {
    const item = await api('/api/telegram/link');
    document.querySelector('#telegram-chat-id').value = item.chat_id || '';
    const status = document.querySelector('#telegram-user-status'), open = document.querySelector('#telegram-open-bot');
    if (item.bot_username) { open.href = `https://t.me/${encodeURIComponent(item.bot_username)}`; open.classList.remove('hidden'); } else open.classList.add('hidden');
    status.textContent = !item.bot_configured ? 'Le notifiche Telegram non sono ancora configurate dall’amministratore.' : item.linked ? `Il tuo account è collegato${item.bot_username ? ` a @${item.bot_username}` : ''}.` : `Il bot è pronto${item.bot_username ? ` (@${item.bot_username})` : ''}. Aprilo, invia /start e incolla qui il Chat ID che ti mostra.`;
    for (const input of telegramPrefs.querySelectorAll('[data-pref]')) input.checked = Boolean(item.preferences?.[input.dataset.pref]);
  } catch (_) {}
}
document.querySelector('#telegram-connect').addEventListener('click', async () => { try { await api('/api/telegram/link',{method:'PUT',body:JSON.stringify({chat_id:document.querySelector('#telegram-chat-id').value.trim()})}); await loadTelegram(); toast('Chat ID Telegram collegato'); } catch(error){ toast(error.message,'error'); } });
document.querySelector('#telegram-test').addEventListener('click', async () => { try { await api('/api/telegram/test',{method:'POST'}); toast('Dashboard Telegram aggiornata'); } catch(error){ toast(error.message,'error'); } });
document.querySelector('#telegram-disconnect').addEventListener('click', async () => { try { await api('/api/telegram/link',{method:'DELETE'}); document.querySelector('#telegram-chat-id').value=''; toast('Telegram scollegato'); } catch(error){ toast(error.message,'error'); } });
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
    versionSelect.innerHTML = versions.map(item=>`<option value="${item.id}" ${item.current?'selected':''}>${item.current?'Corrente · ':''}${new Date(item.completed_at||item.created_at).toLocaleString()} · ${item.message_count.toLocaleString()} messaggi</option>`).join('');
    await loadTransferPreview();
  } catch(error){ transferStatus.textContent=error.message; }
}
async function loadTransferPreview() {
  const accountId=transferForm.elements.account_id.value, snapshotId=transferForm.elements.snapshot_id.value;
  if (!accountId) return;
  try {
    transferPreviewData=await api(`/api/accounts/${accountId}/transfer-preview${snapshotId?`?snapshot_id=${snapshotId}`:''}`);
    renderTransferDestinations();
    const q=transferPreviewData.quota, quota=q.limit==null?'PLUS · ripristini illimitati':`STANDARD · ${q.remaining} di ${q.limit} ripristini disponibili questo mese`;
    document.querySelector('#transfer-preview').innerHTML=`<b>${transferPreviewData.snapshot.messages.toLocaleString()} messaggi · ${bytes(transferPreviewData.snapshot.size)}</b><span>${transferPreviewData.folders.length} ${transferPreviewData.folders.length===1?'cartella':'cartelle'}</span><small>${esc(quota)}</small>`;
  } catch(error){ transferStatus.textContent=error.message; }
}
function renderTransferDestinations() {
  // The server already filtered out mailboxes that cannot receive a restore and
  // tagged each one with the provider that will write into it; the picker only shows real mailboxes.
  const select = transferForm.elements.destination_account_id;
  const items = transferPreviewData?.destinations || [];
  const previous = select.value;
  select.innerHTML = items.map(item=>`<option value="${item.id}">${esc(item.display_name)} · ${esc(item.email)} · ${esc(item.provider_label)}</option>`).join('');
  const providerBadge={microsoft_graph:'M',gmail:'G',imap:'@'};
  const cards=document.querySelector('#transfer-destination-cards');
  cards.innerHTML=items.map(item=>`<label class="choice-card destination-card"><input type="radio" name="destination_choice" value="${item.id}"><span class="account-avatar">${esc(providerBadge[item.provider]||'@')}</span><span><b>${esc(item.display_name)}</b><small>${esc(item.email)}</small><span class="destination-status">${esc(item.provider_label)}</span></span></label>`).join('')
    + `<label class="choice-card destination-card destination-other"><input type="radio" name="destination_choice" value="new"><span class="account-avatar">+</span><span><b>Un’altra casella</b><small>Gmail, Outlook o IMAP personalizzato</small></span></label>`;
  const empty = items.length === 0;
  document.querySelector('#transfer-no-destinations').classList.toggle('hidden', !empty);
  const restored = items.some(item=>String(item.id)===previous) ? previous : (items[0] ? String(items[0].id) : '');
  const wanted = empty ? 'new' : restored;
  const target = cards.querySelector(`[name="destination_choice"][value="${CSS.escape(wanted)}"]`) || cards.querySelector('[name="destination_choice"]');
  if (target) { target.checked = true; select.value = wanted === 'new' ? '' : wanted; }
  const kind = transferForm.querySelector(`[name="destination_kind"][value="${wanted === 'new' ? 'new' : 'existing'}"]`);
  if (kind && !kind.checked) { kind.checked = true; kind.dispatchEvent(new Event('change',{bubbles:true})); }
}
document.querySelector('#transfer-destination-cards').addEventListener('change', event => {
  if (event.target.name !== 'destination_choice') return;
  const isNew = event.target.value === 'new';
  transferForm.elements.destination_account_id.value = isNew ? '' : event.target.value;
  const kind = transferForm.querySelector(`[name="destination_kind"][value="${isNew ? 'new' : 'existing'}"]`);
  kind.checked = true;
  kind.dispatchEvent(new Event('change', {bubbles: true}));
});

function renderTransferReview() {
  const values=Object.fromEntries(new FormData(transferForm));
  const dest=values.destination_kind==='existing' ? transferForm.elements.destination_account_id.selectedOptions[0]?.textContent : values.label;
  const folders=values.mode==='preserve'?'Mantieni le cartelle originali':`Una sola cartella · ${values.single_folder}`;
  document.querySelector('#transfer-review').innerHTML=`<div><span>Archivio</span><b>${esc(transferForm.elements.account_id.selectedOptions[0]?.textContent)}</b></div><div><span>Contenuto</span><b>${transferPreviewData?.snapshot.messages.toLocaleString()||'—'} messaggi · ${bytes(transferPreviewData?.snapshot.size||0)}</b></div><div><span>Destinazione</span><b>${esc(dest||'Non selezionata')}</b></div><div><span>Cartelle</span><b>${esc(folders)}</b></div><small>La quota mensile viene consumata solo quando il ripristino viene accodato.</small>`;
}
async function openTransfer(preferredAccountId=null) {
  const accounts=await api('/api/accounts');
  const sources=accounts.filter(item=>item.has_archive), source=transferForm.elements.account_id;
  source.innerHTML=sources.map(item=>`<option value="${item.id}">${esc(item.display_name)} · ${esc(item.email)}</option>`).join('');
  if (preferredAccountId && [...source.options].some(option=>Number(option.value)===Number(preferredAccountId))) source.value=String(preferredAccountId);
  if (!source.value){ toast('Crea un backup prima di ripristinare in una casella','error'); return; }
  transferStatus.textContent=''; showTransferStep(1); transferDialog.showModal(); await loadTransferSource();
}
async function loadTransferJobs() {
  try {
    const data=await api('/api/imap-transfers'), active=data.items.filter(item=>['queued','running','cancelling'].includes(item.status)), history=data.items.filter(item=>!['queued','running','cancelling'].includes(item.status)).slice(0,5);
    document.querySelector('#transfer-jobs').innerHTML=active.map(item=>`<article><div><b>Restore #${item.id} · ${esc(item.destination_label)}</b><span>${esc(item.current_folder||item.status)} · ${item.processed_messages}/${item.total_messages} · ${item.percent}%${item.eta_seconds!=null?` · ETA ${duration(item.eta_seconds)}`:''}</span></div><div class="progress"><i data-progress="${item.percent}"></i></div><button class="text-button danger-text" type="button" data-cancel-transfer="${item.id}">Annulla ripristino</button></article>`).join('') || '<p class="muted">Nessun ripristino in corso.</p>';
    document.querySelector('#transfer-history').innerHTML=history.length?`<h4>Ripristini recenti</h4>${history.map(item=>`<div><b>${esc(item.destination_label)}</b><span>${esc(item.status)} · ${item.processed_messages}/${item.total_messages}${item.error?` · ${esc(item.error)}`:''}</span></div>`).join('')}`:'';
    applyProgressWidths(document.querySelector('#transfer-jobs'));
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
document.querySelector('#transfer-next').addEventListener('click',async()=>{ transferStatus.textContent=''; if(transferStep===1&&!transferPreviewData){transferStatus.textContent='Scegli una versione dell’archivio valida.';return;} if(transferStep===2){const values=Object.fromEntries(new FormData(transferForm));if(values.destination_kind==='existing'&&!values.destination_account_id){transferStatus.textContent='Scegli la casella di destinazione.';return;}if(values.destination_kind==='new'&&(!values.label||!values.password||!values.imap_host)){transferStatus.textContent='Scegli un provider e inserisci email e app password della casella.';return;}} showTransferStep(transferStep+1); });
document.querySelector('#transfer-back').addEventListener('click',()=>showTransferStep(transferStep-1));
document.querySelector('#nav-transfers').addEventListener('click',()=>openTransfer());
document.querySelector('#transfer-test').addEventListener('click',async()=>{ const result=document.querySelector('#transfer-test-result'); result.textContent='Verifica della connessione…'; try{const data=await api('/api/imap-transfer/test',{method:'POST',body:JSON.stringify({destination:transferDestination()})});result.textContent=`Connessione riuscita · ${data.folders} cartelle · nessuna quota consumata`;result.className='success';}catch(error){result.textContent=error.message;result.className='danger-text';} });
document.querySelector('#transfer-jobs').addEventListener('click',async event=>{const button=event.target.closest('[data-cancel-transfer]');if(!button)return;try{await api(`/api/imap-transfers/${button.dataset.cancelTransfer}/cancel`,{method:'POST'});toast('Annullamento del ripristino richiesto');await Promise.all([loadTransferJobs(),loadWebUsage()]);}catch(error){toast(error.message,'error');}});
transferForm.addEventListener('submit',async event=>{event.preventDefault();const values=Object.fromEntries(new FormData(transferForm));transferStatus.textContent='Verifica e accodamento del ripristino…';try{const result=await api(`/api/accounts/${values.account_id}/transfers`,{method:'POST',body:JSON.stringify({snapshot_id:Number(values.snapshot_id),destination:transferDestination(),mode:values.mode,single_folder:values.mode==='single'?values.single_folder:null,mappings:{},skip_duplicates:Boolean(values.skip_duplicates)})});toast(`Ripristino #${result.job.id} accodato`);showTransferStep(4);await Promise.all([loadTransferJobs(),loadWebUsage()]);}catch(error){transferStatus.textContent=error.message;}});

document.querySelector('#nav-settings').addEventListener('click',loadTelegram);
loadWebUsage(); setInterval(()=>{loadWebUsage();if(transferDialog.open&&transferStep===4)loadTransferJobs();},15000);
