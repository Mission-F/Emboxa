const csrf = document.querySelector('meta[name="csrf-token"]').content;
const state = {accounts: [], account: null, folders: [], versions: [], snapshotId: null, folderId: null, trash: false, deletedCount: 0, archiveView: 'messages', page: 1, pageSize: 50, total: 0, filters: {}, polling: null, openAccountMenuId: null, attachments: {page:1,pageSize:60,total:0,mode:'grid',category:'all',q:''}};
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value = '') => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const iconPaths = {
  dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  inbox: '<path d="M4 7.5 12 13l8-5.5M5 6h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  sent: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  archive: '<path d="M4 7h16v13H4zM3 3h18v4H3zM9 11h6"/>',
  trash: '<path d="M4 7h16M10 11v5m4-5v5M9 4h6l1 3H8l1-3Zm-3 3 1 14h10l1-14"/>',
  star: '<path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z"/>',
  paperclip: '<path d="m21.4 11.6-8.9 8.9a6 6 0 0 1-8.5-8.5l9.5-9.5a4 4 0 0 1 5.7 5.7l-9.6 9.5a2 2 0 0 1-2.8-2.8l8.8-8.8"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  arrowLeft: '<path d="m15 18-6-6 6-6"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 15-5-5L5 20"/>',
  mail: '<path d="M4 7.5 12 13l8-5.5M5 6h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z"/>'
};
const icon = (name, className = '') => `<svg class="${className}" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name]}</svg>`;

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf);
  const response = await fetch(url, {...options, headers});
  if (response.status === 401) { location.href = '/login'; throw new Error('Sessione scaduta'); }
  const type = response.headers.get('content-type') || '';
  const result = type.includes('json') ? await response.json() : null;
  if (!response.ok) throw new Error(result?.detail || `Errore ${response.status}`);
  return result;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message; el.className = `toast show ${kind}`;
  clearTimeout(el._timer); el._timer = setTimeout(() => { el.className = 'toast'; }, 3500);
}
function bytes(value = 0) { const units=['B','KB','MB','GB','TB']; let i=0,n=Number(value); while(n>=1024&&i<4){n/=1024;i++;} return `${n.toFixed(i ? 1 : 0)} ${units[i]}`; }
function date(value) { return value ? new Intl.DateTimeFormat('it-IT',{dateStyle:'medium',timeStyle:'short'}).format(new Date(value)) : 'Mai'; }
function statusLabel(value) { return ({never:'Mai eseguito',running:'Backup in corso',completed:'Backup completato',failed:'Backup non riuscito',cancelled:'Backup interrotto',imported:'Archivio importato',cleared:'Archivio cancellato'})[value] || value; }
function duration(seconds) { if (seconds == null) return 'Stima in corso…'; const n=Math.max(0,Number(seconds)); if(n<60)return `~${Math.max(1,Math.round(n/5)*5)} sec`;if(n<3600)return `~${Math.round(n/60)} min`;const h=Math.floor(n/3600),m=Math.round((n%3600)/300)*5;return `~${h} h${m?` ${m} min`:''}`; }
function emptyReader() { return `<div class="reader-empty"><span>${icon('mail')}</span><p>Seleziona un messaggio per leggerlo</p></div>`; }
function skeleton(count = 6) { return `<div class="skeleton-list">${Array.from({length: count}, () => '<div class="skeleton-row"></div>').join('')}</div>`; }

async function loadAccounts(quiet = false) {
  try {
    const previous = new Map(state.accounts.map(account => [account.id, account]));
    state.accounts = await api('/api/accounts');
    if (quiet) {
      for (const account of state.accounts) {
        const old = previous.get(account.id);
        if (old?.job && !account.job && account.last_backup_status === 'completed') toast(`Backup completato · ${account.message_count.toLocaleString('it-IT')} messaggi`);
        if (old?.job && !account.job && account.last_backup_status === 'failed') toast('Backup non riuscito', 'error');
      }
    }
    renderAccounts();
    await loadActivity(true);
  } catch (error) { if (!quiet) toast(error.message, 'error'); }
}

function accountCard(account) {
  const job = account.job;
  const progress = job ? `<div class="job"><div><span>${esc(job.current_folder || statusLabel(job.status))}</span><strong>${job.status==='queued'?'In coda':`${job.percent}%`}</strong></div><div class="progress"><i style="width:${job.percent}%"></i></div><small>${job.processed_messages.toLocaleString('it-IT')} / ${job.total_messages ? job.total_messages.toLocaleString('it-IT') : '?'} messaggi · ${job.attachment_count.toLocaleString('it-IT')} allegati${job.status==='running'?` · ${job.throughput.toFixed(1)} msg/s · ETA ${duration(job.eta_seconds)}`:''}</small><button data-action="cancel" data-id="${job.id}" class="text-button danger-text">Interrompi</button></div>` : '';
  return `<article class="account-card" data-account-card="${account.id}">
      <div class="card-top"><div class="account-avatar">${esc(account.display_name.charAt(0).toUpperCase())}</div><div class="account-title"><h2>${esc(account.display_name)}</h2><p>${esc(account.email)}</p></div>
      <details class="menu" data-account-menu="${account.id}" ${state.openAccountMenuId===account.id?'open':''}><summary aria-label="Azioni account ${esc(account.display_name)}" aria-haspopup="menu" aria-expanded="${state.openAccountMenuId===account.id?'true':'false'}">${icon('dots')}</summary><div role="menu"><button role="menuitem" data-action="edit" data-id="${account.id}">Modifica IMAP</button>${account.imap_enabled?`<button role="menuitem" data-action="test-saved" data-id="${account.id}">Test connessione</button>`:''}${!account.is_permanent?`<button role="menuitem" data-action="permanent" data-id="${account.id}">Make permanent</button>`:'<span class="permanent-label">Permanent</span>'}${account.has_archive?`<button role="menuitem" data-action="export" data-id="${account.id}">Esporta archivio</button><button role="menuitem" data-action="clear" data-id="${account.id}" class="danger-text">Cancella archivio</button>`:''}<button role="menuitem" data-action="delete" data-id="${account.id}" class="danger-text">Elimina account</button></div></details></div>
      <div class="card-stats"><div><strong>${account.message_count.toLocaleString('it-IT')}</strong><span>messaggi</span></div><div><strong>${bytes(account.archive_size)}</strong><span>archivio</span></div></div>
      <div class="last-backup"><span class="status-dot ${esc(account.last_backup_status)}"></span><div><strong>${esc(statusLabel(account.last_backup_status))}</strong><small>${date(account.last_backup_at)}${account.next_backup_at ? ` · Prossimo ${date(account.next_backup_at)}` : ''}</small></div></div>
      ${account.last_backup_error ? `<p class="card-error" title="${esc(account.last_backup_error)}">${esc(account.last_backup_error)}</p>` : ''}${progress}
      <div class="card-actions"><button data-action="backup" data-id="${account.id}" class="primary" ${!account.imap_enabled||job?'disabled':''}>Backup ora</button><button data-action="open" data-id="${account.id}" class="secondary" ${!account.has_archive?'disabled':''}>Apri archivio</button></div>
    </article>`;
}

function renderAccounts() {
  const grid = $('#account-grid');
  $('#empty-state').classList.toggle('hidden', state.accounts.length !== 0);
  const liveIds = new Set(state.accounts.map(account => String(account.id)));
  for (const oldCard of $$('[data-account-card]', grid)) if (!liveIds.has(oldCard.dataset.accountCard)) oldCard.remove();
  state.accounts.forEach((account, index) => {
    const current = grid.querySelector(`[data-account-card="${account.id}"]`);
    if (current && state.openAccountMenuId === account.id) return;
    const template = document.createElement('template'); template.innerHTML = accountCard(account).trim();
    const next = template.content.firstElementChild;
    if (current) current.replaceWith(next);
    else grid.append(next);
    const positioned = grid.children[index];
    if (positioned !== next) grid.insertBefore(next, positioned || null);
  });
}

function closeAccountMenus({restoreFocus = false} = {}) {
  const open = $('.menu[open]');
  state.openAccountMenuId = null;
  if (open) {
    open.open = false;
    open.querySelector('summary')?.setAttribute('aria-expanded', 'false');
    if (restoreFocus) open.querySelector('summary')?.focus();
  }
}

$('#account-grid').addEventListener('toggle', event => {
  const menu = event.target.closest('[data-account-menu]'); if (!menu) return;
  const id = Number(menu.dataset.accountMenu); const summary = menu.querySelector('summary');
  summary?.setAttribute('aria-expanded', String(menu.open));
  if (!menu.open) { if (state.openAccountMenuId === id) state.openAccountMenuId = null; return; }
  for (const other of $$('.menu[open]', $('#account-grid'))) if (other !== menu) other.open = false;
  state.openAccountMenuId = id;
}, true);

document.addEventListener('keydown', event => { if (event.key === 'Escape' && state.openAccountMenuId !== null) { event.preventDefault(); closeAccountMenus({restoreFocus:true}); } });

function accountPayload(form) {
  const data = Object.fromEntries(new FormData(form));
  delete data.account_id;
  data.imap_port = Number(data.imap_port);
  data.schedule_interval_hours = data.schedule_mode === 'interval' ? Number(data.schedule_interval_hours) : null;
  data.retention_versions = Number(data.retention_versions || 3);
  data.root_folder = data.root_folder || null; data.password = data.password || null;
  return data;
}
function openAccountDialog(account = null) {
  const form = $('#account-form'); form.reset(); form.account_id.value = account?.id || '';
  $('#account-dialog-title').textContent = account ? 'Modifica account' : 'Aggiungi account';
  $('#password-hint').textContent = account ? 'Lascia vuoto per mantenere la password salvata.' : 'Obbligatoria per un nuovo account.';
  if (account) for (const [key,value] of Object.entries(account)) if (form.elements[key] && value !== null) form.elements[key].value = value;
  else { form.imap_port.value = 993; form.security.value = 'ssl'; }
  $('.advanced-settings').open = Boolean(account?.root_folder || (account?.schedule_mode && account.schedule_mode !== 'disabled'));
  $('#interval-label').classList.toggle('hidden', form.schedule_mode.value !== 'interval');
  const status = $('#account-form-status'); status.textContent = ''; status.className = 'form-status';
  $('#account-dialog').showModal();
}

async function confirmAction(title, message) {
  $('#confirm-title').textContent = title; $('#confirm-text').textContent = message;
  const dialog = $('#confirm-dialog'); dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), {once:true}));
}

document.addEventListener('click', async event => {
  if (!event.target.closest('.menu') && state.openAccountMenuId !== null) closeAccountMenus();
  const close = event.target.closest('[data-close]'); if (close) return $(`#${close.dataset.close}`).close();
  const add = event.target.closest('[data-action="add"],#add-account'); if (add) return openAccountDialog();
  const button = event.target.closest('[data-action]'); if (!button) return;
  if (button.closest('.menu')) closeAccountMenus();
  const id = Number(button.dataset.id); const account = state.accounts.find(item => item.id === id);
  try {
    if (button.dataset.action === 'edit') openAccountDialog(account);
    if (button.dataset.action === 'backup') { const result=await api(`/api/accounts/${id}/backup`, {method:'POST'}); toast(result.created?'Backup aggiunto alla coda':'Backup già in coda'); loadAccounts(true); }
    if (button.dataset.action === 'cancel' && await confirmAction('Interrompere il backup?', 'Lo staging incompleto sarà eliminato. Il backup precedente resterà disponibile.')) { await api(`/api/jobs/${id}/cancel`, {method:'POST'}); toast('Interruzione richiesta'); }
    if (button.dataset.action === 'test-saved') { button.disabled=true; const result=await api(`/api/accounts/${id}/test`,{method:'POST'}); toast(`Connessione riuscita · ${result.folders} cartelle`); button.disabled=false; }
    if (button.dataset.action === 'open') await openArchive(account);
    if (button.dataset.action === 'export') location.href = `/api/accounts/${id}/export`;
    if (button.dataset.action === 'permanent' && await confirmAction('Make permanent?', 'Standard plans can change the permanent mailbox only after the 31-day lock.')) { await api(`/api/accounts/${id}/permanent`,{method:'POST'});toast('Permanent mailbox updated');loadAccounts(); }
    if (button.dataset.action === 'clear' && await confirmAction('Cancellare l’archivio locale?', 'La configurazione IMAP resterà salvata, ma EML e allegati locali saranno rimossi.')) { await api(`/api/accounts/${id}/archive`,{method:'DELETE'}); toast('Archivio cancellato'); loadAccounts(); }
    if (button.dataset.action === 'delete' && await confirmAction('Eliminare account e archivio?', 'Questa operazione elimina configurazione, EML e allegati locali. Non modifica il server email.')) { await api(`/api/accounts/${id}`,{method:'DELETE'}); toast('Account eliminato'); loadAccounts(); }
  } catch (error) { button.disabled=false; toast(error.message,'error'); }
});

$('#account-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form=event.currentTarget, id=form.account_id.value, status=$('#account-form-status');
  status.className='form-status'; status.textContent='Salvataggio in corso…';
  try { await api(id?`/api/accounts/${id}`:'/api/accounts',{method:id?'PUT':'POST',body:JSON.stringify(accountPayload(form))}); $('#account-dialog').close(); toast('Account salvato'); loadAccounts(); }
  catch(error){status.textContent=error.message;status.className='form-status error';}
});
$('#test-connection').addEventListener('click', async () => {
  const form=$('#account-form'), status=$('#account-form-status'), id=form.account_id.value, payload=accountPayload(form);
  status.className='form-status'; status.textContent='Verifica della connessione…';
  try {
    let result;
    if (!payload.password && id) result=await api(`/api/accounts/${id}/test`,{method:'POST'});
    else result=await api('/api/accounts/test',{method:'POST',body:JSON.stringify({imap_host:payload.imap_host,imap_port:payload.imap_port,security:payload.security,imap_username:payload.imap_username,password:payload.password})});
    status.textContent=`Connessione riuscita · ${result.folders} cartelle trovate`; status.className='form-status success';
  } catch(error){status.textContent=`Connessione non riuscita. ${error.message}`;status.className='form-status error';}
});
$('#provider-preset').addEventListener('change', event => {
  const presets={gmail:['imap.gmail.com',993,'ssl'],outlook:['outlook.office365.com',993,'ssl'],icloud:['imap.mail.me.com',993,'ssl'],yahoo:['imap.mail.yahoo.com',993,'ssl']};
  const value=presets[event.target.value]; if(value){const form=$('#account-form');form.imap_host.value=value[0];form.imap_port.value=value[1];form.security.value=value[2];}
});
$('#account-form').schedule_mode.addEventListener('change', event => $('#interval-label').classList.toggle('hidden',event.target.value!=='interval'));

function folderIcon(folder) {
  const name = folder.name.toLowerCase();
  if (name.includes('sent') || name.includes('inviat')) return 'sent';
  if (name.includes('trash') || name.includes('cestin')) return 'trash';
  if (name.includes('archive') || name.includes('archiv')) return 'archive';
  if (name === 'inbox' || name.includes('arrivo')) return 'inbox';
  return 'folder';
}
async function openArchive(account) {
  state.account=account; state.folderId=null; state.trash=false; state.archiveView='messages'; state.page=1; state.filters={};
  $('#dashboard').classList.add('hidden'); $('#archive').classList.remove('hidden'); $('#search-wrap').classList.remove('hidden');
  $('#archive-name').textContent=account.display_name; $('#archive-email').textContent=account.email; $('#archive-avatar').textContent=account.display_name.charAt(0).toUpperCase(); $('#reader').innerHTML=emptyReader();
  state.versions=await api(`/api/accounts/${account.id}/versions`); state.snapshotId=state.versions.find(version=>version.current)?.id||state.versions[0]?.id||null; renderVersions();
  state.folders=await api(`/api/accounts/${account.id}/folders?${snapshotParam()}`); renderFolders(); await Promise.all([loadMessages(),loadStats()]);
}
function snapshotParam(){return new URLSearchParams({snapshot_id:state.snapshotId}).toString();}
function renderVersions(){
  $('#version-select').innerHTML=state.versions.map(version=>`<option value="${version.id}" ${version.id===state.snapshotId?'selected':''}>${date(version.completed_at)}${version.current?' · Current':''}${version.protected?' · Protected':''}</option>`).join('');
  const current=state.versions.find(version=>version.id===state.snapshotId),comparison=current?.comparison;
  $('#protection-warning').classList.toggle('hidden',!comparison?.suspicious);
  if(comparison?.suspicious)$('#protection-warning').innerHTML=`<strong>Riduzione anomala rilevata</strong><span>${comparison.messages_removed.toLocaleString('it-IT')} messaggi in meno. La versione precedente è protetta.</span>`;
  $('#versions-content').innerHTML=state.versions.map(version=>`<article class="version-row ${version.current?'current':''}"><div><strong>${date(version.completed_at)}</strong><span>${version.message_count.toLocaleString('it-IT')} messaggi · ${bytes(version.archive_size)} · ${version.attachment_count} allegati</span></div><div class="version-badges">${version.current?'<b>Current</b>':''}${version.protected?'<b class="protected">Protected</b>':''}</div>${version.protection_reason?`<p>${esc(version.protection_reason)}</p><div class="version-actions"><button data-protection="keep" data-id="${version.id}" class="secondary">Keep both</button><button data-protection="replace" data-id="${version.id}" class="danger">Replace old backup</button></div>`:''}${version.comparison?.suspicious?`<dl><div><dt>Messaggi rimossi</dt><dd>−${version.comparison.messages_removed.toLocaleString('it-IT')}</dd></div><div><dt>Cartelle rimosse</dt><dd>${version.comparison.folders_removed.length}</dd></div><div><dt>Allegati</dt><dd>${version.comparison.attachments_difference}</dd></div><div><dt>Dimensione</dt><dd>${bytes(Math.abs(version.comparison.size_difference))}${version.comparison.size_difference<0?' in meno':' in più'}</dd></div></dl>`:''}</article>`).join('');
}
function renderFolders(){
  $('#folder-list').innerHTML=`<button class="folder-item ${!state.trash&&state.folderId===null?'active':''}" data-folder="">${icon('mail')}<b>Tutti i messaggi</b><em>${state.account.message_count.toLocaleString('it-IT')}</em></button>`+state.folders.map(folder=>`<button class="folder-item ${!state.trash&&state.folderId===folder.id?'active':''}" data-folder="${folder.id}">${icon(folderIcon(folder))}<b>${esc(folder.name)}</b><em>${folder.message_count.toLocaleString('it-IT')}</em></button>`).join('')+`<button class="folder-item ${state.trash?'active':''}" data-folder="trash">${icon('trash')}<b>Archive Trash</b><em>${state.deletedCount.toLocaleString('it-IT')}</em></button>`;
}
$('#folder-list').addEventListener('click',event=>{const button=event.target.closest('[data-folder]');if(!button)return;state.trash=button.dataset.folder==='trash';state.folderId=!state.trash&&button.dataset.folder?Number(button.dataset.folder):null;state.page=1;renderFolders();showArchiveView('messages');loadMessages();$('#folder-sidebar').classList.remove('open');});
async function loadStats(){try{const stats=await api(`/api/accounts/${state.account.id}/stats?${snapshotParam()}`);state.deletedCount=stats.deleted||0;$('#archive-stats').innerHTML=`<span>${stats.folders} cartelle</span><span>${stats.attachments} allegati</span><span>${bytes(stats.archive_size)}</span>`;renderFolders();}catch(error){toast(error.message,'error');}}
function queryString(){const params=new URLSearchParams({page:state.page,page_size:state.pageSize,snapshot_id:state.snapshotId,trash:state.trash,...state.filters});if(state.folderId)params.set('folder_id',state.folderId);const search=$('#search-input').value.trim();if(search)params.set('q',search);return params;}
async function loadMessages(){
  $('#message-list').innerHTML=skeleton();
  try {
    const data=await api(`/api/accounts/${state.account.id}/messages?${queryString()}`); state.total=data.total;
    $('#result-count').textContent=`${data.total.toLocaleString('it-IT')} risultati`; $('#list-title').textContent=state.trash?'Archive Trash':state.folderId?(state.folders.find(folder=>folder.id===state.folderId)?.name||'Cartella'):'Tutti i messaggi';
    $('#message-list').innerHTML=data.items.length?data.items.map(message=>`<button class="message-row ${message.is_read?'':'unread'}" data-message="${message.id}"><span class="star ${message.is_starred?'active':''}">${icon('star')}</span><span class="message-main"><span class="message-line"><strong>${esc(message.sender||'(mittente sconosciuto)')}</strong><time>${date(message.date)}</time></span><span class="message-subject">${esc(message.subject)} ${message.has_attachments?icon('paperclip'):''}</span><span class="snippet">${esc(message.snippet)}</span></span></button>`).join(''):`<div class="empty-list">${icon('mail')}<p>Nessun messaggio corrisponde alla ricerca.</p></div>`;
    const pages=Math.max(1,Math.ceil(data.total/state.pageSize));$('#page-label').textContent=`${state.page} di ${pages}`;$('#prev-page').disabled=state.page<=1;$('#next-page').disabled=state.page>=pages;
  } catch(error){$('#message-list').innerHTML=`<div class="empty-list error">${esc(error.message)}</div>`;}
}
$('#message-list').addEventListener('click',async event=>{const row=event.target.closest('[data-message]');if(!row)return;$$('.message-row').forEach(item=>item.classList.remove('selected'));row.classList.add('selected');await readThread(Number(row.dataset.message));});
async function readThread(id){
  const reader=$('#reader');reader.innerHTML=skeleton(4);
  try {
    const thread=await api(`/api/messages/${id}/thread`);
    reader.innerHTML=`<header class="reader-heading"><button class="mobile-reader-back ghost" type="button">${icon('arrowLeft')}<span>Messaggi</span></button><div><p class="eyebrow">CONVERSAZIONE · ${thread.length}</p><h1>${esc(thread.at(-1)?.subject)}</h1></div></header>`+thread.map(message=>`<section class="thread-message"><details ${message.id===id||thread.length===1?'open':''}><summary><div><strong>${esc(message.sender||'(sconosciuto)')}</strong><span>${date(message.date)} · ${esc(message.folder)}</span></div>${icon('chevronDown','thread-chevron')}</summary><div class="message-toolbar">${message.has_html?`<button class="ghost" data-message-action="remote" data-id="${message.id}" type="button">Load remote images</button>`:''}${message.is_deleted?`<button class="secondary" data-message-action="restore" data-id="${message.id}" type="button">Restore</button><button class="danger" data-message-action="permanent" data-id="${message.id}" type="button">Delete permanently</button>`:`<button class="danger ghost-danger" data-message-action="trash" data-id="${message.id}" type="button">Delete from archive</button>`}</div><div class="message-meta"><div><b>A:</b> ${esc(message.to||'—')}</div>${message.cc?`<div><b>CC:</b> ${esc(message.cc)}</div>`:''}${message.bcc?`<div><b>BCC:</b> ${esc(message.bcc)}</div>`:''}</div>${message.has_html?`<iframe id="mail-frame-${message.id}" class="mail-frame" sandbox="allow-same-origin" src="${message.render_url}" title="Contenuto email"></iframe>`:`<div class="mail-body"><pre>${esc(message.text_body)}</pre></div>`}${renderAttachments(message)}<div class="raw-link"><a href="${message.raw_url}">Scarica messaggio originale .eml</a></div></details></section>`).join('');
    reader.scrollTop=0;
  } catch(error){reader.innerHTML=`<div class="reader-empty error">${esc(error.message)}</div>`;}
}
function renderAttachments(message){
  const files=message.attachments.filter(attachment=>!attachment.is_inline); if(!files.length)return'';
  return`<div class="attachments"><h3>Allegati · ${files.length}</h3><div>${files.map(attachment=>`<button type="button" data-open-attachment='${esc(JSON.stringify({...attachment,message_id:message.id,subject:message.subject,sender:message.sender,folder:message.folder,date:message.date}))}'>${icon(attachment.content_type.startsWith('image/')?'image':'file')}<b>${esc(attachment.filename)}</b><small>${bytes(attachment.size)}</small></button>`).join('')}</div></div>`;
}
$('#reader').addEventListener('click',async event=>{
  if(event.target.closest('.mobile-reader-back'))return $('#reader').innerHTML=emptyReader();
  const attachment=event.target.closest('[data-open-attachment]');if(attachment)return openAttachmentViewer(JSON.parse(attachment.dataset.openAttachment));
  const button=event.target.closest('[data-message-action]');if(!button)return;
  const id=Number(button.dataset.id),action=button.dataset.messageAction;
  try{
    if(action==='remote'){const frame=$(`#mail-frame-${id}`);frame.src=`/api/messages/${id}/render?remote_images=1`;button.remove();return;}
    if(action==='trash'&&await confirmAction('Eliminare questa email dall’archivio?', 'Non verrà eliminata dalla mailbox originale.'))await api(`/api/messages/${id}/trash`,{method:'POST'});
    if(action==='restore')await api(`/api/messages/${id}/restore`,{method:'POST'});
    if(action==='permanent'&&await confirmAction('Eliminare definitivamente?', 'Il messaggio verrà rimosso solo da questa versione locale.'))await api(`/api/messages/${id}/permanent`,{method:'DELETE'});
    toast(action==='restore'?'Email ripristinata':action==='trash'?'Email spostata in Archive Trash':'Email eliminata definitivamente');$('#reader').innerHTML=emptyReader();await Promise.all([loadMessages(),loadStats()]);
  }catch(error){toast(error.message,'error');}
});
let searchTimer;$('#search-input').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.page=1;loadMessages();},350)});
$('#prev-page').addEventListener('click',()=>{if(state.page>1){state.page--;loadMessages();}});$('#next-page').addEventListener('click',()=>{if(state.page*state.pageSize<state.total){state.page++;loadMessages();}});
$('#filters-button').addEventListener('click',()=>$('#filters-dialog').showModal());
$('#filters-form').addEventListener('submit',event=>{event.preventDefault();const raw=Object.fromEntries(new FormData(event.currentTarget));state.filters=Object.fromEntries(Object.entries(raw).filter(([,value])=>value));state.page=1;$('#filters-button').classList.toggle('active',Object.keys(state.filters).length>0);$('#filters-dialog').close();loadMessages();});
$('#clear-filters').addEventListener('click',()=>{$('#filters-form').reset();state.filters={};state.page=1;$('#filters-button').classList.remove('active');$('#filters-dialog').close();loadMessages();});
function showArchiveView(view){
  state.archiveView=view;const attachments=view==='attachments';
  $('.message-column').classList.toggle('hidden',attachments);$('#reader').classList.toggle('hidden',attachments);$('#attachments-view').classList.toggle('hidden',!attachments);
  $('#messages-view-button').classList.toggle('active',!attachments);$('#attachments-view-button').classList.toggle('active',attachments);
  if(attachments){state.trash=false;renderFolders();loadAttachments();}
}
$('#messages-view-button').addEventListener('click',()=>showArchiveView('messages'));
$('#attachments-view-button').addEventListener('click',()=>showArchiveView('attachments'));

function attachmentData(item){return esc(JSON.stringify(item));}
function attachmentPreview(item){return item.category==='images'?`<img loading="lazy" src="${item.open_url}" alt="">`:`<div class="file-tile ${item.category}">${icon(item.category==='pdf'?'file':item.category==='images'?'image':'archive')}<b>${esc((item.extension||'FILE').toUpperCase())}</b></div>`;}
async function loadAttachments(){
  const container=$('#attachments-results'),a=state.attachments;container.innerHTML=skeleton(8);
  try{const params=new URLSearchParams({snapshot_id:state.snapshotId,page:a.page,page_size:a.pageSize,category:a.category});if(a.q)params.set('q',a.q);if(state.folderId)params.set('folder_id',state.folderId);const data=await api(`/api/accounts/${state.account.id}/attachments?${params}`);a.total=data.total;$('#attachments-count').textContent=`${data.total.toLocaleString('it-IT')} file`;container.className=`attachments-results ${a.mode}`;container.innerHTML=data.items.length?data.items.map(item=>`<article class="attachment-card">${attachmentPreview(item)}<div class="attachment-info"><h3 title="${esc(item.filename)}">${esc(item.filename)}</h3><p>${esc((item.extension||item.category).toUpperCase())} · ${bytes(item.size)}</p><dl><div><dt>From</dt><dd>${esc(item.sender||'—')}</dd></div><div><dt>Subject</dt><dd>${esc(item.subject)}</dd></div><div><dt>Folder</dt><dd>${esc(item.folder)}</dd></div><div><dt>Date</dt><dd>${date(item.date)}</dd></div></dl></div><div class="attachment-card-actions"><button class="secondary" type="button" data-view-attachment='${attachmentData(item)}'>Open</button><a class="ghost" href="${item.download_url}" download>Download</a><button class="ghost" type="button" data-show-email="${item.message_id}">Show email</button></div></article>`).join(''):`<div class="empty-list">${icon('file')}<p>Nessun allegato corrisponde ai filtri.</p></div>`;const pages=Math.max(1,Math.ceil(data.total/a.pageSize));$('#attachments-page').textContent=`${a.page} di ${pages}`;$('#attachments-prev').disabled=a.page<=1;$('#attachments-next').disabled=a.page>=pages;}catch(error){container.innerHTML=`<div class="empty-list error">${esc(error.message)}</div>`;}
}
async function openAttachmentViewer(item){
  const dialog=$('#attachment-viewer'),mime=(item.content_type||'').toLowerCase(),extension=(item.extension||item.filename?.split('.').pop()||'').toLowerCase(),url=item.open_url||`/api/attachments/${item.id}?inline=1`;
  $('#viewer-filename').textContent=item.filename;$('#viewer-meta').textContent=`${mime||extension||'File'} · ${bytes(item.size)}`;$('#viewer-download').href=item.download_url||`/api/attachments/${item.id}`;$('#viewer-show-email').dataset.messageId=item.message_id||'';
  const content=$('#viewer-content');content.innerHTML='<div class="loading">Apertura anteprima…</div>';dialog.showModal();
  if(mime.startsWith('image/')||['jpg','jpeg','png','webp','gif','svg'].includes(extension))content.innerHTML=`<img class="viewer-image" src="${url}" alt="${esc(item.filename)}">`;
  else if(mime==='application/pdf'||extension==='pdf')content.innerHTML=`<iframe class="viewer-frame" src="${url}" title="PDF ${esc(item.filename)}"></iframe>`;
  else if(mime.startsWith('audio/'))content.innerHTML=`<audio controls src="${url}"></audio>`;
  else if(mime.startsWith('video/'))content.innerHTML=`<video controls src="${url}"></video>`;
  else if(mime.startsWith('text/')||['txt','csv','json','xml','log','md','yaml','yml'].includes(extension)){try{const response=await fetch(item.text_preview_url||`/api/attachments/${item.id}/text-preview`);if(!response.ok)throw new Error('Anteprima non disponibile');content.innerHTML=`<pre class="viewer-text">${esc(await response.text())}</pre>`;}catch(error){content.innerHTML=`<div class="viewer-unsupported">${icon('file')}<p>${esc(error.message)}</p><small>Il file resta disponibile tramite Download.</small></div>`;}}
  else content.innerHTML=`<div class="viewer-unsupported">${icon('file')}<h3>${esc(item.filename)}</h3><p>${esc(mime||'Formato non supportato')}</p><small>Anteprima locale non disponibile. Usa Download per aprire il file con un’app compatibile.</small></div>`;
}
async function showEmailFromAttachment(messageId){$('#attachment-viewer').close();showArchiveView('messages');state.trash=false;state.folderId=null;state.page=1;renderFolders();await loadMessages();await readThread(Number(messageId));}
$('#attachments-results').addEventListener('click',event=>{const open=event.target.closest('[data-view-attachment]');if(open)return openAttachmentViewer(JSON.parse(open.dataset.viewAttachment));const show=event.target.closest('[data-show-email]');if(show)return showEmailFromAttachment(show.dataset.showEmail);});
$('#viewer-show-email').addEventListener('click',event=>showEmailFromAttachment(event.currentTarget.dataset.messageId));
let attachmentSearchTimer;$('#attachment-search').addEventListener('input',event=>{clearTimeout(attachmentSearchTimer);attachmentSearchTimer=setTimeout(()=>{state.attachments.q=event.target.value.trim();state.attachments.page=1;loadAttachments();},300);});
$('#attachment-filter').addEventListener('change',event=>{state.attachments.category=event.target.value;state.attachments.page=1;loadAttachments();});
$('#attachments-grid-button').addEventListener('click',()=>{state.attachments.mode='grid';$('#attachments-grid-button').classList.add('active');$('#attachments-list-button').classList.remove('active');loadAttachments();});
$('#attachments-list-button').addEventListener('click',()=>{state.attachments.mode='list';$('#attachments-list-button').classList.add('active');$('#attachments-grid-button').classList.remove('active');loadAttachments();});
$('#attachments-prev').addEventListener('click',()=>{if(state.attachments.page>1){state.attachments.page--;loadAttachments();}});$('#attachments-next').addEventListener('click',()=>{if(state.attachments.page*state.attachments.pageSize<state.attachments.total){state.attachments.page++;loadAttachments();}});
function showDashboard(){state.account=null;$('#archive').classList.add('hidden');$('#dashboard').classList.remove('hidden');$('#search-wrap').classList.add('hidden');$('#search-input').value='';$('.folder-sidebar').classList.remove('open');loadAccounts();}
$('#archive-back').addEventListener('click',showDashboard);$('#home-button').addEventListener('click',showDashboard);
$('#mobile-folders').addEventListener('click',()=>{const target=state.account?$('#folder-sidebar'):$('.main-sidebar');target.classList.toggle('open');});
$('#nav-accounts').addEventListener('click',()=>{$('#account-grid').scrollIntoView({behavior:'smooth',block:'start'});$('.main-sidebar').classList.remove('open');});
$('#nav-archives').addEventListener('click',()=>{$('#account-grid').scrollIntoView({behavior:'smooth',block:'start'});toast('Apri un archivio dalla relativa scheda');$('.main-sidebar').classList.remove('open');});
$('#nav-settings').addEventListener('click',()=>{$('#settings-dialog').showModal();$('.main-sidebar').classList.remove('open');});

$('#version-select').addEventListener('change',async event=>{state.snapshotId=Number(event.target.value);state.folderId=null;state.trash=false;state.page=1;state.attachments.page=1;renderVersions();state.folders=await api(`/api/accounts/${state.account.id}/folders?${snapshotParam()}`);renderFolders();await loadStats();if(state.archiveView==='attachments')await loadAttachments();else await loadMessages();});
$('#versions-button').addEventListener('click',()=>{$('#versions-dialog').showModal();renderVersions();});
$('#versions-dialog').addEventListener('click',async event=>{const button=event.target.closest('[data-protection]');if(!button)return;try{await api(`/api/snapshots/${button.dataset.id}/protection`,{method:'POST',body:JSON.stringify({action:button.dataset.protection})});toast(button.dataset.protection==='keep'?'Versione protetta conservata':'Protezione rimossa e retention applicata');state.versions=await api(`/api/accounts/${state.account.id}/versions`);renderVersions();}catch(error){toast(error.message,'error');}});

async function loadActivity(quiet=false){
  try{
    const activity=await api('/api/backup-activity'),active=activity.running+activity.queued>0;
    $('#backup-activity-button').classList.toggle('hidden',!active);$('#activity-summary').textContent=`${activity.running} running · ${activity.queued} queued`;
    $('#activity-content').innerHTML=activity.jobs.length?activity.jobs.map((job,index)=>`<article class="activity-job ${job.status}"><div class="activity-state"><b>${job.status==='queued'?`NEXT ${index+1}`:job.status.toUpperCase()}</b><span>${job.percent}%</span></div><h3>${esc(job.account.email)}</h3><p>${esc(job.current_folder||statusLabel(job.status))}</p><div class="progress"><i style="width:${job.percent}%"></i></div><div class="activity-metrics"><span>${job.processed_messages.toLocaleString('it-IT')} / ${job.total_messages?.toLocaleString('it-IT')||'?'} messaggi</span><span>${job.throughput?`${job.throughput.toFixed(1)} msg/s`:''}</span><span>${job.status==='running'?`ETA ${duration(job.eta_seconds)}`:''}</span></div><button data-action="cancel" data-id="${job.id}" class="secondary">Cancel</button></article>`).join(''):'<div class="empty-list"><p>Nessun backup attivo o in coda.</p></div>';
  }catch(error){if(!quiet)toast(error.message,'error');}
}
$('#backup-activity-button').addEventListener('click',()=>{$('#activity-dialog').showModal();loadActivity();});
const collapsed=localStorage.getItem('emboxa-sidebar-collapsed')==='true';document.body.classList.toggle('sidebar-collapsed',collapsed);
$('#sidebar-collapse').addEventListener('click',()=>{document.body.classList.toggle('sidebar-collapsed');localStorage.setItem('emboxa-sidebar-collapsed',document.body.classList.contains('sidebar-collapsed'));});

$('#import-button').addEventListener('click',()=>$('#import-input').click());
$('#import-input').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;const data=new FormData();data.append('file',file);toast('Verifica e importazione in corso…');try{await api('/api/import',{method:'POST',body:data});toast('Archivio importato');loadAccounts();}catch(error){toast(error.message,'error');}finally{event.target.value='';}});
$('#logout-button').addEventListener('click',async()=>{await api('/api/logout',{method:'POST'});location.href='/login';});

const themeMedia = matchMedia('(prefers-color-scheme: dark)');
function applyTheme(preference) {
  const resolved = preference === 'system' ? (themeMedia.matches ? 'dark' : 'light') : preference;
  document.documentElement.dataset.theme = resolved;
  $('#theme-select').value = preference;
  localStorage.setItem('emboxa-theme', preference);
}
const savedTheme = localStorage.getItem('emboxa-theme') || localStorage.getItem('mailvault-theme') || 'light';
applyTheme(savedTheme);
$('#theme-select').addEventListener('change',event=>applyTheme(event.target.value));
themeMedia.addEventListener('change',()=>{if((localStorage.getItem('emboxa-theme')||'system')==='system')applyTheme('system');});

let preferences={locale:'auto',tutorial_completed:true},tourIndex=0;
const tourSteps=[
  {selector:'#add-account',title:'addTitle',copy:'addCopy'},
  {selector:'[data-action="backup"]',title:'backupTitle',copy:'backupCopy'},
  {selector:'#backup-activity-button',title:'activityTitle',copy:'activityCopy'},
  {selector:'[data-action="open"]',title:'archiveTitle',copy:'archiveCopy'},
  {selector:'[data-action="export"]',title:'exportTitle',copy:'exportCopy'}
];
function localize(){EMBOXA_I18N.apply(preferences.locale);$('#language-select').value=preferences.locale;$('#settings-language').value=preferences.locale;}
async function savePreferences(values){preferences=await api('/api/preferences',{method:'PATCH',body:JSON.stringify(values)});localize();}
function hideTour(){const pop=$('#tour-popover');pop.classList.add('hidden');$$('.tour-target').forEach(el=>el.classList.remove('tour-target'));}
function showTourStep(){hideTour();while(tourIndex<tourSteps.length&&!$(tourSteps[tourIndex].selector))tourIndex++;if(tourIndex>=tourSteps.length){savePreferences({tutorial_completed:true});return;}const step=tourSteps[tourIndex],target=$(step.selector),pop=$('#tour-popover');target.classList.add('tour-target');$('#tour-title').textContent=EMBOXA_I18N.t(step.title,preferences.locale);$('#tour-copy').textContent=EMBOXA_I18N.t(step.copy,preferences.locale);pop.classList.remove('hidden');const rect=target.getBoundingClientRect();pop.style.left=`${Math.max(12,Math.min(innerWidth-pop.offsetWidth-12,rect.left))}px`;pop.style.top=`${Math.min(innerHeight-pop.offsetHeight-12,rect.bottom+10)}px`;}
function startTour(){tourIndex=0;setTimeout(showTourStep,200);}
$('#welcome-start').addEventListener('click',()=>{$('#welcome-dialog').close();startTour();});
$('#welcome-skip').addEventListener('click',async()=>{await savePreferences({tutorial_completed:true});$('#welcome-dialog').close();});
$('#tour-next').addEventListener('click',()=>{tourIndex++;showTourStep();});
$('#tour-skip').addEventListener('click',()=>{hideTour();savePreferences({tutorial_completed:true});});
$('#tour-never').addEventListener('click',()=>{hideTour();savePreferences({tutorial_completed:true});});
$('#restart-tutorial').addEventListener('click',async()=>{await savePreferences({tutorial_completed:false});$('#settings-dialog').close();$('#welcome-dialog').showModal();});
async function setLocale(value){await savePreferences({locale:value});}
$('#language-select').addEventListener('change',event=>setLocale(event.target.value));
$('#settings-language').addEventListener('change',event=>setLocale(event.target.value));
async function initExperience(){preferences=await api('/api/preferences');localize();if(!preferences.tutorial_completed)$('#welcome-dialog').showModal();}

loadAccounts();initExperience();
state.polling=setInterval(()=>{loadActivity(true);loadAccounts(true);},2500);
