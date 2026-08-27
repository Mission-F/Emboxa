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
  link: '<path d="M10 13a5 5 0 0 0 7.1 0l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.1 0l-2 2a5 5 0 0 0 7.1 7.1l1.1-1.1"/>',
  upload: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M5 17v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2"/>',
  server: '<rect x="4" y="4" width="16" height="6" rx="2"/><rect x="4" y="14" width="16" height="6" rx="2"/><path d="M8 7h.01M8 17h.01"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 15-5-5L5 20"/>',
  mail: '<path d="M4 7.5 12 13l8-5.5M5 6h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2Z"/>',
  transfer: '<path d="M7 7h11m0 0-3-3m3 3-3 3M17 17H6m0 0 3 3m-3-3 3-3"/>',
  download: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M5 19h14"/>'
};
const icon = (name, className = '') => `<svg class="${className}" viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name]}</svg>`;
/* Every user-facing string in this file goes through t(); preferences.locale (set further down,
   before any of this is ever called) decides it/en. */
const t = (key) => (window.EMBOXA_I18N ? EMBOXA_I18N.t(key, preferences.locale) : key);
const uiLocale = () => (window.EMBOXA_I18N ? EMBOXA_I18N.resolve(preferences.locale) : 'it') === 'it' ? 'it-IT' : 'en-US';
const numberFmt = (value) => Number(value).toLocaleString(uiLocale());

// The app CSP forbids inline style attributes, so bar widths are applied through the CSSOM.
function applyProgressWidths(root) {
  for (const bar of (root || document).querySelectorAll('[data-progress]')) {
    bar.style.width = `${Math.max(0, Math.min(100, Number(bar.dataset.progress) || 0))}%`;
  }
}
async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf);
  const response = await fetch(url, {...options, headers});
  if (response.status === 401) { location.href = '/login'; throw new Error(t('sessionExpired')); }
  const type = response.headers.get('content-type') || '';
  const result = type.includes('json') ? await response.json() : null;
  if (!response.ok) throw new Error(result?.detail || `${t('errorPrefix')} ${response.status}`);
  return result;
}

function b64urlToBuffer(value) {
  const padded = `${value}${'='.repeat((4 - value.length % 4) % 4)}`.replace(/-/g, '+').replace(/_/g, '/');
  const binary = atob(padded);
  return Uint8Array.from(binary, char => char.charCodeAt(0)).buffer;
}
function bufferToB64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  bytes.forEach(byte => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}
function credentialToJSON(credential) {
  const response = {clientDataJSON: bufferToB64url(credential.response.clientDataJSON)};
  if (credential.response.attestationObject) response.attestationObject = bufferToB64url(credential.response.attestationObject);
  if (credential.response.authenticatorData) response.authenticatorData = bufferToB64url(credential.response.authenticatorData);
  if (credential.response.signature) response.signature = bufferToB64url(credential.response.signature);
  if (credential.response.userHandle) response.userHandle = bufferToB64url(credential.response.userHandle);
  if (credential.response.getTransports) response.transports = credential.response.getTransports();
  return {
    id: credential.id,
    rawId: bufferToB64url(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || undefined,
    clientExtensionResults: credential.getClientExtensionResults?.() || {},
    response,
  };
}
function registrationOptionsFromJSON(options) {
  const publicKey = {...options, challenge: b64urlToBuffer(options.challenge), user: {...options.user, id: b64urlToBuffer(options.user.id)}};
  if (publicKey.excludeCredentials) {
    publicKey.excludeCredentials = publicKey.excludeCredentials.map(item => ({...item, id: b64urlToBuffer(item.id)}));
  }
  return publicKey;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message; el.className = `toast show ${kind}`;
  clearTimeout(el._timer); el._timer = setTimeout(() => { el.className = 'toast'; }, 3500);
}
function bytes(value = 0) { const units=['B','KB','MB','GB','TB']; let i=0,n=Number(value); while(n>=1024&&i<4){n/=1024;i++;} return `${n.toFixed(i ? 1 : 0)} ${units[i]}`; }
function date(value) { return value ? new Intl.DateTimeFormat(uiLocale(),{dateStyle:'medium',timeStyle:'short'}).format(new Date(value)) : t('never'); }
/* Maps a backup status onto the design system's badge tones, so status reads as a colour
   before it reads as a word. */
/* "Name <addr@host>" -> "Name"; a bare address keeps its local part. Mailbox rows read as people,
   the full address stays available in the reader and in the row's title attribute. */
/* IMAP folder paths are flat strings ("INBOX/Clienti"). The sidebar shows the leaf name and
   indents by depth so the hierarchy is visible without restructuring the API payload. */
function folderDepth(name) { return Math.min(3, ((name || '').match(/[\/.]/g) || []).length); }
function folderLeaf(name) { const parts = (name || '').split(/[\/.]/); return parts[parts.length - 1] || name; }

function senderName(value) {
  const raw = (value || '').trim();
  if (!raw) return '';
  const angled = raw.match(/^\s*(.*?)\s*<([^>]+)>\s*$/);
  const name = angled ? angled[1].replace(/^["']|["']$/g, '').trim() : '';
  if (name) return name;
  const address = angled ? angled[2] : raw;
  return address.split('@')[0] || address;
}
function senderInitial(value) { const name = senderName(value); return (name.match(/[\p{L}\p{N}]/u) || ['?'])[0].toUpperCase(); }

function statusTone(value) { return ({completed:'success',imported:'success',running:'accent running',failed:'danger',cancelled:'warning',cleared:'warning',disconnected:'danger'})[value] || ''; }
function statusLabel(value) { return ({never:t('statusNever'),running:t('statusRunning'),completed:t('statusCompleted'),failed:t('statusFailed'),cancelled:t('statusCancelled'),imported:t('statusImported'),cleared:t('statusCleared'),disconnected:t('statusDisconnected')})[value] || value; }
function duration(seconds) { if (seconds == null) return t('estimating'); const n=Math.max(0,Number(seconds)); if(n<60)return `~${Math.max(1,Math.round(n/5)*5)} sec`;if(n<3600)return `~${Math.round(n/60)} min`;const h=Math.floor(n/3600),m=Math.round((n%3600)/300)*5;return `~${h} h${m?` ${m} min`:''}`; }
function emptyReader() { return `<div class="reader-empty"><span>${icon('mail')}</span><p>${t('selectMessage')}</p></div>`; }
function skeleton(count = 6) { return `<div class="skeleton-list">${Array.from({length: count}, () => '<div class="skeleton-row"></div>').join('')}</div>`; }

async function loadAccounts(quiet = false) {
  try {
    const previous = new Map(state.accounts.map(account => [account.id, account]));
    state.accounts = await api('/api/accounts');
    if (quiet) {
      for (const account of state.accounts) {
        const old = previous.get(account.id);
        if (old?.job && !account.job && account.last_backup_status === 'completed') toast(`${t('toastBackupCompleted')} · ${numberFmt(account.message_count)} ${t('messagesUnit')}`);
        if (old?.job && !account.job && account.last_backup_status === 'failed') toast(t('toastBackupFailed'), 'error');
      }
    }
    renderAccounts();
    await loadActivity(true);
  } catch (error) { if (!quiet) toast(error.message, 'error'); }
}

function accountCard(account) {
  const job = account.job;
  const microsoft = account.auth_provider === 'microsoft';
  const mbox = account.auth_provider === 'mbox';
  const progress = job ? `<div class="job"><div><span>${esc(job.current_folder || statusLabel(job.status))}</span><strong>${job.status==='queued'?t('inQueue'):`${job.percent}%`}</strong></div><div class="progress"><i data-progress="${job.percent}"></i></div><small>${numberFmt(job.processed_messages)} / ${job.total_messages ? numberFmt(job.total_messages) : '?'} ${t('messagesUnit')} · ${numberFmt(job.attachment_count)} ${t('attachmentsUnit')}${job.status==='running'?` · ${job.throughput.toFixed(1)} msg/s · ETA ${duration(job.eta_seconds)}`:''}</small><button data-action="cancel" data-id="${job.id}" class="text-button danger-text">${t('interrupt')}</button></div>` : '';
  return `<article class="account-card" data-account-card="${account.id}">
      <div class="card-top"><div class="ds-avatar lg account-avatar">${microsoft?'M':mbox?'B':esc(account.display_name.charAt(0).toUpperCase())}</div><div class="account-title"><h2>${esc(account.display_name)}</h2><p>${esc(account.email)}</p></div>
      <details class="menu" data-account-menu="${account.id}" ${state.openAccountMenuId===account.id?'open':''}><summary aria-label="${t('accountActionsAria')} ${esc(account.display_name)}" aria-haspopup="menu" aria-expanded="${state.openAccountMenuId===account.id?'true':'false'}">${icon('dots')}</summary><div role="menu">${microsoft||mbox?'':`<button role="menuitem" data-action="edit" data-id="${account.id}">${t('editImap')}</button>`}<button role="menuitem" data-action="retention" data-id="${account.id}">${t('retentionVersionsAction')}</button>${account.imap_enabled?`<button role="menuitem" data-action="test-saved" data-id="${account.id}">${t('testConnection')}</button>`:''}${microsoft?`<button role="menuitem" data-action="disconnect-microsoft" data-id="${account.id}" class="danger-text">${t('disconnectMicrosoft')}</button>`:''}${!account.is_permanent?`<button role="menuitem" data-action="permanent" data-id="${account.id}">${t('makePermanent')}</button>`:`<span class="permanent-label">${t('permanentLabel')}</span>`}${account.has_archive?`<button role="menuitem" data-action="export" data-id="${account.id}">${t('exportArchiveAction')}</button><button role="menuitem" data-action="export-local" data-id="${account.id}">${t('exportToNas')}</button><button role="menuitem" data-action="clear" data-id="${account.id}" class="danger-text">${t('clearArchive')}</button>`:''}<button role="menuitem" data-action="delete" data-id="${account.id}" class="danger-text">${t('deleteAccount')}</button></div></details></div>
      <div class="card-tags"><span class="ds-badge plain ${microsoft?'accent':''}">${microsoft?t('providerMicrosoft'):mbox?t('providerMboxOffline'):t('providerImap')}</span>${account.is_permanent?`<span class="ds-badge plain accent">${t('permanentLabel')}</span>`:''}${account.imap_enabled?'':`<span class="ds-badge plain">${t('readOnly')}</span>`}</div>
      <div class="card-stats"><div><strong>${numberFmt(account.message_count)}</strong><span>${t('messagesUnit')}</span></div><div><strong>${bytes(account.archive_size)}</strong><span>${t('archiveUnit')}</span></div></div>
      <div class="last-backup"><span class="ds-badge ${statusTone(account.last_backup_status)}">${esc(statusLabel(account.last_backup_status))}</span><small>${account.last_backup_at ? date(account.last_backup_at) : t('neverRun')}${account.next_backup_at ? ` · ${t('prossimoPrefix')} ${date(account.next_backup_at)}` : ''}</small></div>
      ${account.last_backup_error ? `<p class="card-error" title="${esc(account.last_backup_error)}">${esc(account.last_backup_error)}</p>` : ''}${progress}
      <div class="card-actions"><button data-action="open" data-id="${account.id}" class="primary" ${!account.has_archive?'disabled':''}>${t('openArchive')}</button><button data-action="backup" data-id="${account.id}" class="secondary" ${!account.imap_enabled||job?'disabled':''}>${job?t('backupRunning'):t('backupNow')}</button>${account.has_archive?`<button data-action="transfer" data-id="${account.id}" class="secondary icon-only" title="${t('restoreToMailboxAction')}" aria-label="${t('restoreToMailboxAction')}">${icon('transfer')}</button>`:''}</div>
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
    applyProgressWidths(next);
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
$('#microsoft-connect')?.addEventListener('click', () => { location.href = '/api/auth/microsoft/start'; });
// Microsoft offers two paths: OAuth (default, recommended) and manual IMAP behind "Avanzate".
let microsoftManual = false, microsoftConfigured = null;
async function checkMicrosoftOAuth() {
  if (microsoftConfigured !== null) return microsoftConfigured;
  try { microsoftConfigured = Boolean((await api('/api/auth/microsoft/status')).configured); }
  catch (_) { microsoftConfigured = false; }
  return microsoftConfigured;
}
$('#microsoft-manual')?.addEventListener('click', () => { microsoftManual = !microsoftManual; syncProviderMode(); });
function syncProviderMode() {
  const microsoft = $('#provider-preset')?.value === 'outlook';
  const oauthOnly = microsoft && !microsoftManual;
  $('#microsoft-oauth-box')?.classList.toggle('hidden', !microsoft);
  $('#microsoft-manual')?.classList.toggle('active', microsoftManual);
  if ($('#microsoft-manual')) $('#microsoft-manual').textContent = microsoftManual
    ? t('backToMicrosoftConnect')
    : t('advancedManualImap');
  $$('.imap-field').forEach(node => {
    node.classList.toggle('hidden', oauthOnly);
    $$('input,select,textarea', node).forEach(field => {
      field.disabled = oauthOnly;
      if (field.name !== 'password') field.required = !oauthOnly;
    });
  });
  $('#account-form .advanced-settings')?.classList.toggle('hidden', oauthOnly);
  $('#test-connection')?.classList.toggle('hidden', oauthOnly);
  $('#save-account')?.classList.toggle('hidden', oauthOnly);
  const status = $('#account-form-status');
  if (microsoft && status) {
    status.className = 'form-status';
    status.textContent = oauthOnly
      ? t('microsoftRecommendedStatus')
      : t('microsoftAdvancedStatus');
  }
  if (microsoft) checkMicrosoftOAuth().then(configured => {
    // Without OAuth credentials the button would only produce an error: steer to IMAP instead.
    $('#microsoft-connect').disabled = !configured;
    if (!configured && !microsoftManual) { microsoftManual = true; syncProviderMode(); }
    if (!configured && status) status.textContent = t('microsoftOauthUnavailable');
  });
}

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
  $('#account-dialog-title').textContent = account ? t('editAccountTitle') : t('addAccount');
  $('#password-hint').textContent = account ? t('passwordHintOptional') : t('passwordHintRequired');
  if (account) for (const [key,value] of Object.entries(account)) if (form.elements[key] && value !== null) form.elements[key].value = value;
  else { form.imap_port.value = 993; form.security.value = 'ssl'; }
  $('.advanced-settings').open = Boolean(account?.root_folder || (account?.schedule_mode && account.schedule_mode !== 'disabled'));
  $('#interval-label').classList.toggle('hidden', form.schedule_mode.value !== 'interval');
  microsoftManual = Boolean(account);
  syncProviderMode();
  const status = $('#account-form-status'); status.textContent = ''; status.className = 'form-status';
  $('#account-dialog').showModal();
}

async function confirmAction(title, message) {
  $('#confirm-title').textContent = title; $('#confirm-text').textContent = message;
  const dialog = $('#confirm-dialog'); dialog.showModal();
  return new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), {once:true}));
}

const exportStepOrder = {prepare: 1, browser: 2, save: 3};
function exportStep(step, status = 'active') {
  $$('[data-export-step]').forEach(node => {
    const current = node.dataset.exportStep;
    const done = exportStepOrder[current] < exportStepOrder[step] || (current === step && status === 'done');
    node.classList.toggle('active', current === step && status === 'active');
    node.classList.toggle('done', done);
  });
}
function exportProgress(status, percent = 0, detail = '') {
  $('#export-status').textContent = status;
  $('#export-percent').textContent = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
  $('#export-progress-bar').style.width = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
  $('#export-detail').textContent = detail;
}
function triggerBrowserDownload(url, filename) {
  const link = document.createElement('a');
  link.href = url; link.download = filename; link.rel = 'noopener'; link.style.display = 'none';
  document.body.append(link); link.click(); link.remove();
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitForExportJob(job) {
  let current = job, visualPercent = Number(job.percent || 5);
  while (current.status === 'queued' || current.status === 'running') {
    visualPercent = Math.min(99, Math.max(visualPercent + 1, Number(current.percent || 0)));
    exportProgress(current.status === 'queued' ? t('exportQueuedNote') : t('exportBackgroundNote'), visualPercent, current.detail || t('serverPreparingFile'));
    await wait(1500);
    current = await api(current.status_url);
  }
  if (current.status === 'failed') throw new Error(current.error || current.detail || t('exportFailedGeneric'));
  if (!current.export?.download_url) throw new Error(t('exportDownloadUnavailable'));
  return current.export;
}
async function exportArchive(accountId) {
  const dialog = $('#export-dialog');
  $('#export-close').disabled = true; $('#export-done').disabled = true;
  $$('[data-export-step]').forEach(node => node.classList.remove('active', 'done'));
  $('#export-summary').textContent = t('creatingPackageBackground');
  exportStep('prepare'); exportProgress(t('startingAsyncExport'), 5, t('asyncExportNote'));
  dialog.showModal();
  try {
    const job = await api(`/api/accounts/${accountId}/export`, {method:'POST'});
    const info = await waitForExportJob(job);
    exportStep('prepare', 'done');
    $('#export-summary').textContent = `${info.filename} · ${bytes(info.size)}`;
    exportProgress(t('localExportReady'), 72, info.persistent ? t('retentionNoExpiry') : `${t('retentionUntil')} ${date(info.expires_at)}.`);
    exportStep('browser');
    exportStep('browser', 'done');
    exportStep('save');
    exportProgress(t('downloadStartedBrowser'), 96, t('browserDownloadNote'));
    triggerBrowserDownload(info.download_url, info.filename);
    exportStep('save', 'done');
    exportProgress(t('exportCompleted'), 100, t('exportCompletedNote'));
    toast(t('toastExportCompleted'));
  } catch (error) {
    exportProgress(t('exportNotCompleted'), 0, error.message);
    toast(error.message, 'error');
  } finally {
    $('#export-close').disabled = false; $('#export-done').disabled = false;
  }
}
async function exportArchiveToNas(accountId) {
  const dialog = $('#export-dialog');
  $('#export-close').disabled = true; $('#export-done').disabled = true;
  $$('[data-export-step]').forEach(node => node.classList.remove('active', 'done'));
  $('#export-summary').textContent = t('savingNasPackage');
  exportStep('prepare'); exportProgress(t('startingNasExport'), 5, t('nasExportNote'));
  dialog.showModal();
  try {
    const job = await api(`/api/accounts/${accountId}/export/local`, {method:'POST'});
    let current = job;
    while (current.status === 'queued' || current.status === 'running') {
      exportProgress(current.status === 'queued' ? t('nasExportQueued') : t('nasExportRunning'), current.percent || 5, current.detail || t('preparingFile'));
      await wait(1500);
      current = await api(current.status_url);
    }
    if (current.status === 'failed') throw new Error(current.error || current.detail || t('exportFailedGeneric'));
    exportStep('prepare', 'done'); exportStep('browser', 'done'); exportStep('save', 'done');
    exportProgress(t('nasExportCompleted'), 100, current.local_path ? `${t('fileSavedAt')} ${current.local_path}` : t('fileSavedFolder'));
    toast(t('toastExportSavedNas'));
  } catch (error) {
    exportProgress(t('exportNotCompleted'), 0, error.message);
    toast(error.message, 'error');
  } finally {
    $('#export-close').disabled = false; $('#export-done').disabled = false;
  }
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
    if (button.dataset.action === 'backup') { const result=await api(`/api/accounts/${id}/backup`, {method:'POST'}); toast(result.created?t('toastQueued'):t('toastAlreadyQueued')); loadAccounts(true); }
    if (button.dataset.action === 'retention') {
      const raw = prompt(t('retentionPrompt'), account.retention_versions || 3);
      if (raw === null) return;
      const retention = Number(raw);
      if (!Number.isInteger(retention) || retention < 1 || retention > 100) throw new Error(t('retentionRangeError'));
      await api(`/api/accounts/${id}/settings`, {method:'PATCH', body:JSON.stringify({retention_versions:retention})});
      toast(t('toastRetentionUpdated')); loadAccounts();
    }
    if (button.dataset.action === 'cancel' && await confirmAction(t('confirmCancelBackupTitle'), t('confirmCancelBackupCopy'))) { await api(`/api/jobs/${id}/cancel`, {method:'POST'}); toast(t('toastInterruptRequested')); }
    if (button.dataset.action === 'test-saved') { button.disabled=true; const result=await api(`/api/accounts/${id}/test`,{method:'POST'}); toast(`${t('toastConnectionOk')} · ${result.folders} ${t('folderPlural')}`); button.disabled=false; }
    if (button.dataset.action === 'disconnect-microsoft' && await confirmAction(t('confirmDisconnectMicrosoftTitle'), t('confirmDisconnectMicrosoftCopy'))) { await api(`/api/accounts/${id}/microsoft`,{method:'DELETE'}); toast(t('toastMicrosoftDisconnected')); loadAccounts(); }
    if (button.dataset.action === 'open') await openArchive(account);
    if (button.dataset.action === 'transfer') await openTransfer(id);
    if (button.dataset.action === 'export') await exportArchive(id);
    if (button.dataset.action === 'export-local') await exportArchiveToNas(id);
    if (button.dataset.action === 'permanent' && await confirmAction(t('confirmPermanentTitle'), t('confirmPermanentCopy'))) { await api(`/api/accounts/${id}/permanent`,{method:'POST'});toast(t('toastPermanentUpdated'));loadAccounts(); }
    if (button.dataset.action === 'clear' && await confirmAction(t('confirmClearArchiveTitle'), t('confirmClearArchiveCopy'))) { await api(`/api/accounts/${id}/archive`,{method:'DELETE'}); toast(t('toastArchiveCleared')); loadAccounts(); }
    if (button.dataset.action === 'delete' && await confirmAction(t('confirmDeleteAccountTitle'), t('confirmDeleteAccountCopy'))) { await api(`/api/accounts/${id}`,{method:'DELETE'}); toast(t('toastAccountDeleted')); loadAccounts(); }
  } catch (error) { button.disabled=false; toast(error.message,'error'); }
});

$('#account-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form=event.currentTarget, id=form.account_id.value, status=$('#account-form-status');
  if ($('#provider-preset')?.value === 'outlook' && !microsoftManual) {
    status.className = 'form-status';
    status.textContent = t('microsoftUseButtonHint');
    return;
  }
  status.className='form-status'; status.textContent=t('savingInProgress');
  try { await api(id?`/api/accounts/${id}`:'/api/accounts',{method:id?'PUT':'POST',body:JSON.stringify(accountPayload(form))}); $('#account-dialog').close(); toast(t('toastAccountSaved')); loadAccounts(); }
  catch(error){status.textContent=error.message;status.className='form-status error';}
});
$('#test-connection').addEventListener('click', async () => {
  const form=$('#account-form'), status=$('#account-form-status'), id=form.account_id.value, payload=accountPayload(form);
  status.className='form-status'; status.textContent=t('verifyingConnection');
  try {
    let result;
    if (!payload.password && id) result=await api(`/api/accounts/${id}/test`,{method:'POST'});
    else result=await api('/api/accounts/test',{method:'POST',body:JSON.stringify({imap_host:payload.imap_host,imap_port:payload.imap_port,security:payload.security,imap_username:payload.imap_username,password:payload.password})});
    status.textContent=`${t('toastConnectionOk')} · ${result.folders} ${t('connectionOkFolders')}`; status.className='form-status success';
  } catch(error){status.textContent=`${t('connectionFailed')} ${error.message}`;status.className='form-status error';}
});
$('#provider-preset').addEventListener('change', event => {
  microsoftManual = false;
  syncProviderMode();
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
  $('#mobile-mailbox-label').textContent=account.display_name; $('#mobile-mailbox-label').classList.remove('hidden');
  state.versions=await api(`/api/accounts/${account.id}/versions`); state.snapshotId=state.versions.find(version=>version.current)?.id||state.versions[0]?.id||null; renderVersions();
  state.folders=await api(`/api/accounts/${account.id}/folders?${snapshotParam()}`); renderFolders(); await Promise.all([loadMessages(),loadStats()]);
}
function snapshotParam(){return new URLSearchParams({snapshot_id:state.snapshotId}).toString();}
function renderVersions(){
  $('#version-select').innerHTML=state.versions.map(version=>`<option value="${version.id}" ${version.id===state.snapshotId?'selected':''}>${date(version.completed_at)}${version.current?t('currentSuffix'):''}${version.protected?t('protectedSuffix'):''}</option>`).join('');
  const current=state.versions.find(version=>version.id===state.snapshotId),comparison=current?.comparison;
  $('#protection-warning').classList.toggle('hidden',!comparison?.suspicious);
  if(comparison?.suspicious)$('#protection-warning').innerHTML=`<strong>${t('anomalyDetected')}</strong><span>${numberFmt(comparison.messages_removed)} ${t('anomalyDetail')}</span>`;
  $('#versions-content').innerHTML=state.versions.map(version=>`<article class="version-row ${version.current?'current':''}"><div><strong>${date(version.completed_at)}</strong><span>${numberFmt(version.message_count)} ${t('messagesUnit')} · ${bytes(version.archive_size)} · ${version.attachment_count} ${t('attachmentsUnit')}</span></div><div class="version-badges">${version.current?`<b>${t('currentBadge')}</b>`:''}${version.protected?`<b class="protected">${t('protectedBadge')}</b>`:''}</div>${version.protection_reason?`<p>${esc(version.protection_reason)}</p><div class="version-actions"><button data-protection="keep" data-id="${version.id}" class="secondary">${t('keepBoth')}</button><button data-protection="replace" data-id="${version.id}" class="danger">${t('replaceOldBackup')}</button></div>`:''}${version.comparison?.suspicious?`<dl><div><dt>${t('removedMessages')}</dt><dd>−${numberFmt(version.comparison.messages_removed)}</dd></div><div><dt>${t('removedFolders')}</dt><dd>${version.comparison.folders_removed.length}</dd></div><div><dt>${t('attachmentsHeading')}</dt><dd>${version.comparison.attachments_difference}</dd></div><div><dt>${t('sizeLabel')}</dt><dd>${bytes(Math.abs(version.comparison.size_difference))}${version.comparison.size_difference<0?t('lessSuffix'):t('moreSuffix')}</dd></div></dl>`:''}</article>`).join('');
}
function renderFolders(){
  $('#folder-list').innerHTML=`<button class="folder-item ${!state.trash&&state.folderId===null?'active':''}" data-folder="">${icon('mail')}<b>${t('allMessages')}</b><em>${numberFmt(state.account.message_count)}</em></button>`+state.folders.map(folder=>`<button class="folder-item depth-${folderDepth(folder.name)} ${!state.trash&&state.folderId===folder.id?'active':''}" data-folder="${folder.id}" title="${esc(folder.name)}">${icon(folderIcon(folder))}<b>${esc(folderLeaf(folder.name))}</b><em>${numberFmt(folder.message_count)}</em></button>`).join('')+`<button class="folder-item ${state.trash?'active':''}" data-folder="trash">${icon('trash')}<b>${t('trashFolder')}</b><em>${numberFmt(state.deletedCount)}</em></button>`;
}
$('#folder-list').addEventListener('click',event=>{const button=event.target.closest('[data-folder]');if(!button)return;state.trash=button.dataset.folder==='trash';state.folderId=!state.trash&&button.dataset.folder?Number(button.dataset.folder):null;state.page=1;renderFolders();showArchiveView('messages');loadMessages();$('#folder-sidebar').classList.remove('open');});
async function loadStats(){try{const stats=await api(`/api/accounts/${state.account.id}/stats?${snapshotParam()}`);state.deletedCount=stats.deleted||0;$('#archive-stats').innerHTML=`<span>${stats.folders} ${stats.folders===1?t('folderSingular'):t('folderPlural')}</span><span>${stats.attachments} ${stats.attachments===1?t('attachmentSingular'):t('attachmentPlural')}</span><span>${bytes(stats.archive_size)}</span>`;renderFolders();}catch(error){toast(error.message,'error');}}
function queryString(){const params=new URLSearchParams({page:state.page,page_size:state.pageSize,snapshot_id:state.snapshotId,trash:state.trash,...state.filters});if(state.folderId)params.set('folder_id',state.folderId);const search=$('#search-input').value.trim();if(search)params.set('q',search);return params;}
async function loadMessages(){
  $('#message-list').innerHTML=skeleton();
  try {
    const data=await api(`/api/accounts/${state.account.id}/messages?${queryString()}`); state.total=data.total;
    $('#result-count').textContent=`${numberFmt(data.total)} ${t('results')}`; $('#list-title').textContent=state.trash?t('trashFolder'):state.folderId?(state.folders.find(folder=>folder.id===state.folderId)?.name||t('folderSingular')):t('allMessages');
    $('#message-list').innerHTML=data.items.length?data.items.map(message=>`<button class="message-row ${message.is_read?'':'unread'}" data-message="${message.id}" title="${esc(message.sender||'')}"><span class="row-avatar" aria-hidden="true">${esc(senderInitial(message.sender))}</span><span class="message-main"><span class="message-line"><strong>${esc(senderName(message.sender)||t('unknownSender'))}</strong><time>${date(message.date)}</time></span><span class="message-subject">${esc(message.subject)||t('noSubject')}</span><span class="snippet">${esc(message.snippet)}</span></span><span class="row-marks">${message.has_attachments?icon('paperclip'):''}<span class="star ${message.is_starred?'active':''}">${icon('star')}</span></span></button>`).join(''):`<div class="empty-list">${icon('mail')}<p>${t('noSearchResults')}</p></div>`;
    const pages=Math.max(1,Math.ceil(data.total/state.pageSize));$('#page-label').textContent=`${state.page} ${t('pageOfSeparator')} ${pages}`;$('#prev-page').disabled=state.page<=1;$('#next-page').disabled=state.page>=pages;
  } catch(error){$('#message-list').innerHTML=`<div class="empty-list error">${esc(error.message)}</div>`;}
}
$('#message-list').addEventListener('click',async event=>{const row=event.target.closest('[data-message]');if(!row)return;$$('.message-row').forEach(item=>item.classList.remove('selected'));row.classList.add('selected');await readThread(Number(row.dataset.message));});
async function readThread(id){
  const reader=$('#reader');reader.innerHTML=skeleton(4);
  try {
    const thread=await api(`/api/messages/${id}/thread`);
    reader.innerHTML=`<header class="reader-heading"><button class="mobile-reader-back ghost" type="button">${icon('arrowLeft')}<span>${t('backToMessages')}</span></button><div><p class="eyebrow">${t('conversationEyebrow')} · ${thread.length}</p><h1>${esc(thread.at(-1)?.subject)}</h1></div></header>`+thread.map(message=>`<section class="thread-message"><details ${message.id===id||thread.length===1?'open':''}><summary><span class="row-avatar" aria-hidden="true">${esc(senderInitial(message.sender))}</span><div><strong>${esc(senderName(message.sender)||t('unknownSender'))}</strong><span>${date(message.date)} · ${esc(folderLeaf(message.folder))}</span></div>${icon('chevronDown','thread-chevron')}</summary><div class="message-toolbar">${message.has_html?`<button class="ghost" data-message-action="remote" data-id="${message.id}" type="button">${t('loadRemoteImages')}</button>`:''}${message.is_deleted?`<button class="secondary" data-message-action="restore" data-id="${message.id}" type="button">${t('restoreMessage')}</button><button class="danger" data-message-action="permanent" data-id="${message.id}" type="button">${t('deletePermanently')}</button>`:`<button class="danger ghost-danger" data-message-action="trash" data-id="${message.id}" type="button">${t('moveToTrash')}</button>`}</div><div class="message-meta"><div><b>${t('toField')}</b> ${esc(message.to||'—')}</div>${message.cc?`<div><b>${t('ccField')}</b> ${esc(message.cc)}</div>`:''}${message.bcc?`<div><b>${t('bccField')}</b> ${esc(message.bcc)}</div>`:''}</div>${message.has_html?`<iframe id="mail-frame-${message.id}" class="mail-frame" sandbox="allow-same-origin" src="${message.render_url}" title="${esc(message.subject||'')}"></iframe>`:`<div class="mail-body"><pre>${esc(message.text_body)}</pre></div>`}${renderAttachments(message)}<div class="raw-link"><a href="${message.raw_url}">${icon('download')}${t('downloadOriginalEml')}</a></div></details></section>`).join('');
    reader.scrollTop=0;
  } catch(error){reader.innerHTML=`<div class="reader-empty error">${esc(error.message)}</div>`;}
}
function renderAttachments(message){
  const files=message.attachments.filter(attachment=>!attachment.is_inline); if(!files.length)return'';
  return`<div class="attachments"><h3>${t('attachmentsHeading')} · ${files.length}</h3><div>${files.map(attachment=>{const item={...attachment,message_id:message.id,subject:message.subject,sender:message.sender,folder:message.folder,date:message.date};const label=extensionLabel(item);return`<button type="button" class="${extensionClass(label)}" data-open-attachment='${attachmentData(item)}'>${attachmentPreview(item)}<b title="${esc(attachment.filename)}">${esc(attachment.filename)}</b><small>${label} · ${bytes(attachment.size)}</small></button>`;}).join('')}</div></div>`;
}
$('#reader').addEventListener('click',async event=>{
  if(event.target.closest('.mobile-reader-back'))return $('#reader').innerHTML=emptyReader();
  const attachment=event.target.closest('[data-open-attachment]');if(attachment)return openAttachmentViewer(JSON.parse(attachment.dataset.openAttachment));
  const button=event.target.closest('[data-message-action]');if(!button)return;
  const id=Number(button.dataset.id),action=button.dataset.messageAction;
  try{
    if(action==='remote'){const frame=$(`#mail-frame-${id}`);frame.src=`/api/messages/${id}/render?remote_images=1`;button.remove();return;}
    if(action==='trash'&&await confirmAction(t('confirmDeleteEmailTitle'), t('confirmDeleteEmailCopy')))await api(`/api/messages/${id}/trash`,{method:'POST'});
    if(action==='restore')await api(`/api/messages/${id}/restore`,{method:'POST'});
    if(action==='permanent'&&await confirmAction(t('confirmDeletePermanentTitle'), t('confirmDeletePermanentCopy')))await api(`/api/messages/${id}/permanent`,{method:'DELETE'});
    toast(action==='restore'?t('toastEmailRestored'):action==='trash'?t('toastEmailTrashed'):t('toastEmailDeleted'));$('#reader').innerHTML=emptyReader();await Promise.all([loadMessages(),loadStats()]);
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
function extensionLabel(item) {
  return String(item.extension || item.filename?.split('.').pop() || item.category || 'file').replace(/[^a-z0-9]/gi,'').slice(0,5).toUpperCase() || 'FILE';
}
function extensionClass(label) {
  const key = String(label || '').toUpperCase();
  if (key === 'PDF') return 'file-kind-pdf';
  if (['DOC','DOCX','ODT','RTF','DOCUMENTS'].includes(key)) return 'file-kind-doc';
  if (['XLS','XLSX','ODS','CSV','NUMB','SPREADSHEETS'].includes(key)) return 'file-kind-sheet';
  if (['PPT','PPTX','KEY'].includes(key)) return 'file-kind-slide';
  if (['JPG','JPEG','PNG','GIF','WEBP','SVG','IMAGE','IMAGES'].includes(key)) return 'file-kind-image';
  if (['ZIP','RAR','7Z','TAR','GZ'].includes(key)) return 'file-kind-archive';
  if (['MP3','WAV','M4A'].includes(key)) return 'file-kind-audio';
  if (['MP4','MOV','AVI'].includes(key)) return 'file-kind-video';
  if (['EML','MSG'].includes(key)) return 'file-kind-mail';
  return 'file-kind-other';
}
function attachmentPreview(item){
  const label = extensionLabel(item);
  return `<div class="file-tile extension-tile ${extensionClass(label)}"><b>${esc(label)}</b></div>`;
}
function attachmentCard(item) {
  const label = extensionLabel(item);
  return `<article class="attachment-card ${extensionClass(label)}" role="button" tabindex="0" data-view-attachment='${attachmentData(item)}'>${attachmentPreview(item)}<div class="attachment-info"><h3 title="${esc(item.filename)}">${esc(item.filename)}</h3><p>${label} · ${bytes(item.size)}</p></div></article>`;
}
async function loadAttachments(){
  const container=$('#attachments-results'),a=state.attachments;container.innerHTML=skeleton(8);
  try{const params=new URLSearchParams({snapshot_id:state.snapshotId,page:a.page,page_size:a.pageSize,category:a.category});if(a.q)params.set('q',a.q);if(state.folderId)params.set('folder_id',state.folderId);const data=await api(`/api/accounts/${state.account.id}/attachments?${params}`);a.total=data.total;$('#attachments-count').textContent=`${numberFmt(data.total)} file`;container.className=`attachments-results ${a.mode}`;container.innerHTML=data.items.length?data.items.map(attachmentCard).join(''):`<div class="empty-list">${icon('file')}<p>${t('noAttachmentsMatch')}</p></div>`;const pages=Math.max(1,Math.ceil(data.total/a.pageSize));$('#attachments-page').textContent=`${a.page} ${t('pageOfSeparator')} ${pages}`;$('#attachments-prev').disabled=a.page<=1;$('#attachments-next').disabled=a.page>=pages;}catch(error){container.innerHTML=`<div class="empty-list error">${esc(error.message)}</div>`;}
}
async function openAttachmentViewer(item){
  const dialog=$('#attachment-viewer'),mime=(item.content_type||'').toLowerCase(),extension=(item.extension||item.filename?.split('.').pop()||'').toLowerCase(),url=item.open_url||`/api/attachments/${item.id}?inline=1`;
  $('#viewer-filename').textContent=item.filename;$('#viewer-meta').textContent=`${mime||extension||t('fileFallback')} · ${bytes(item.size)}`;$('#viewer-download').href=item.download_url||`/api/attachments/${item.id}`;$('#viewer-show-email').dataset.messageId=item.message_id||'';
  const content=$('#viewer-content');content.innerHTML=`<div class="loading">${t('openingPreview')}</div>`;dialog.showModal();
  if(mime.startsWith('image/')||['jpg','jpeg','png','webp','gif','svg'].includes(extension))content.innerHTML=`<img class="viewer-image" src="${url}" alt="${esc(item.filename)}">`;
  else if(mime==='application/pdf'||extension==='pdf')content.innerHTML=`<iframe class="viewer-frame" src="${url}" title="PDF ${esc(item.filename)}"></iframe>`;
  else if(mime.startsWith('audio/'))content.innerHTML=`<audio controls src="${url}"></audio>`;
  else if(mime.startsWith('video/'))content.innerHTML=`<video controls src="${url}"></video>`;
  else if(mime.startsWith('text/')||['txt','csv','json','xml','log','md','yaml','yml'].includes(extension)){try{const response=await fetch(item.text_preview_url||`/api/attachments/${item.id}/text-preview`);if(!response.ok)throw new Error(t('previewUnavailable'));content.innerHTML=`<pre class="viewer-text">${esc(await response.text())}</pre>`;}catch(error){content.innerHTML=`<div class="viewer-unsupported">${icon('file')}<p>${esc(error.message)}</p><small>${t('fileAvailableViaDownload')}</small></div>`;}}
  else content.innerHTML=`<div class="viewer-unsupported">${icon('file')}<h3>${esc(item.filename)}</h3><p>${esc(mime||t('unsupportedFormat'))}</p><small>${t('localPreviewUnavailable')}</small></div>`;
}
async function showEmailFromAttachment(messageId){$('#attachment-viewer').close();showArchiveView('messages');state.trash=false;state.folderId=null;state.page=1;renderFolders();await loadMessages();await readThread(Number(messageId));}
$('#attachments-results').addEventListener('click',event=>{const show=event.target.closest('[data-show-email]');if(show)return showEmailFromAttachment(show.dataset.showEmail);if(event.target.closest('a'))return;const open=event.target.closest('[data-view-attachment]');if(open)return openAttachmentViewer(JSON.parse(open.dataset.viewAttachment));});
$('#attachments-results').addEventListener('keydown',event=>{if(!['Enter',' '].includes(event.key))return;const open=event.target.closest('[data-view-attachment]');if(!open)return;event.preventDefault();openAttachmentViewer(JSON.parse(open.dataset.viewAttachment));});
$('#viewer-show-email').addEventListener('click',event=>showEmailFromAttachment(event.currentTarget.dataset.messageId));
let attachmentSearchTimer;$('#attachment-search').addEventListener('input',event=>{clearTimeout(attachmentSearchTimer);attachmentSearchTimer=setTimeout(()=>{state.attachments.q=event.target.value.trim();state.attachments.page=1;loadAttachments();},300);});
$('#attachment-filter').addEventListener('change',event=>{state.attachments.category=event.target.value;state.attachments.page=1;loadAttachments();});
$('#attachments-grid-button').addEventListener('click',()=>{state.attachments.mode='grid';$('#attachments-grid-button').classList.add('active');$('#attachments-list-button').classList.remove('active');loadAttachments();});
$('#attachments-list-button').addEventListener('click',()=>{state.attachments.mode='list';$('#attachments-list-button').classList.add('active');$('#attachments-grid-button').classList.remove('active');loadAttachments();});
$('#attachments-prev').addEventListener('click',()=>{if(state.attachments.page>1){state.attachments.page--;loadAttachments();}});$('#attachments-next').addEventListener('click',()=>{if(state.attachments.page*state.attachments.pageSize<state.attachments.total){state.attachments.page++;loadAttachments();}});
function showDashboard(){state.account=null;$('#archive').classList.add('hidden');$('#dashboard').classList.remove('hidden');$('#search-wrap').classList.add('hidden');$('#search-input').value='';$('.folder-sidebar').classList.remove('open');$('#mobile-mailbox-label').classList.add('hidden');loadAccounts();}
$('#mobile-mailbox-label').addEventListener('click',()=>$('#folder-sidebar').classList.toggle('open'));
$('#archive-back').addEventListener('click',showDashboard);$('#home-button').addEventListener('click',showDashboard);
$('#mobile-folders').addEventListener('click',()=>{const target=state.account?$('#folder-sidebar'):$('.main-sidebar');target.classList.toggle('open');});
$('#nav-dashboard').addEventListener('click',()=>{showDashboard();$('#account-grid').scrollIntoView({behavior:'smooth',block:'start'});$('.main-sidebar').classList.remove('open');});
async function loadPasskeys() {
  const list = $('#passkey-list');
  if (!list) return;
  list.innerHTML = `<p class="muted">${t('loadingPasskeys')}</p>`;
  try {
    const passkeys = await api('/api/passkeys');
    list.innerHTML = passkeys.length ? passkeys.map(item => `<article class="passkey-item">
      <div><b>${esc(item.name || 'Passkey')}</b><small>${item.last_used_at ? `${t('lastUsed')} ${date(item.last_used_at)}` : `${t('created')} ${date(item.created_at)}`}${item.backed_up ? ` · ${t('synced')}` : ''}</small></div>
      <button class="ghost danger-text" type="button" data-passkey-delete="${item.id}">${t('removePasskey')}</button>
    </article>`).join('') : `<p class="muted">${t('noPasskeysRegistered')}</p>`;
  } catch (error) {
    list.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  }
}
async function addPasskey() {
  const status = $('#passkey-status');
  if (!window.PublicKeyCredential || !navigator.credentials) {
    status.textContent = t('passkeyNotSupported');
    return;
  }
  status.textContent = t('openingBrowserPrompt');
  try {
    const options = await api('/api/passkeys/register/options', {method:'POST'});
    const credential = await navigator.credentials.create({publicKey: registrationOptionsFromJSON(options)});
    if (!credential) throw new Error(t('registrationCancelled'));
    await api('/api/passkeys/register/verify', {method:'POST', body:JSON.stringify({credential: credentialToJSON(credential), name: 'Passkey'})});
    status.textContent = '';
    toast(t('toastPasskeyAdded'));
    loadPasskeys();
  } catch (error) {
    status.textContent = error.message;
  }
}
$('#nav-settings').addEventListener('click',()=>{$('#settings-dialog').showModal();$('.main-sidebar').classList.remove('open');loadPasskeys();});
$('#passkey-add')?.addEventListener('click', addPasskey);
$('#passkey-list')?.addEventListener('click', async event => {
  const button = event.target.closest('[data-passkey-delete]');
  if (!button) return;
  try {
    await api(`/api/passkeys/${button.dataset.passkeyDelete}`, {method:'DELETE'});
    toast(t('toastPasskeyRemoved'));
    loadPasskeys();
  } catch (error) {
    toast(error.message, 'error');
  }
});

$('#version-select').addEventListener('change',async event=>{state.snapshotId=Number(event.target.value);state.folderId=null;state.trash=false;state.page=1;state.attachments.page=1;renderVersions();state.folders=await api(`/api/accounts/${state.account.id}/folders?${snapshotParam()}`);renderFolders();await loadStats();if(state.archiveView==='attachments')await loadAttachments();else await loadMessages();});
$('#versions-button').addEventListener('click',()=>{$('#versions-dialog').showModal();renderVersions();});
$('#versions-dialog').addEventListener('click',async event=>{const button=event.target.closest('[data-protection]');if(!button)return;try{await api(`/api/snapshots/${button.dataset.id}/protection`,{method:'POST',body:JSON.stringify({action:button.dataset.protection})});toast(button.dataset.protection==='keep'?'Versione protetta conservata':'Protezione rimossa e retention applicata');state.versions=await api(`/api/accounts/${state.account.id}/versions`);renderVersions();}catch(error){toast(error.message,'error');}});

const processIcons={backup:'inbox',transfer:'transfer',import:'upload'};
function processStageLabel(status){ return ({queued:t('queuedState'),running:t('runningState'),cancelling:t('cancellingState')})[status]; }
function processCard(item){
  const metrics=[];
  if(item.total!=null)metrics.push(`<span>${numberFmt(item.processed||0)} / ${numberFmt(item.total)} ${t('messagesUnit')}</span>`);
  if(item.eta_seconds!=null&&item.status==='running')metrics.push(`<span>ETA ${duration(item.eta_seconds)}</span>`);
  return `<article class="activity-job ${item.status}">
    <div class="activity-state"><span class="activity-kind">${icon(processIcons[item.kind]||'file')}<b>${esc(processStageLabel(item.status)||item.status.toUpperCase())}</b></span><span>${item.percent}%</span></div>
    <h3>${esc(item.label)}</h3><p>${esc(item.detail||'')}</p>
    <div class="progress"><i data-progress="${item.percent}"></i></div>
    ${metrics.length?`<div class="activity-metrics">${metrics.join('')}</div>`:''}
    ${item.error?`<p class="card-error">${esc(item.error)}</p>`:''}
    ${item.cancel_url?`<button data-cancel-process="${esc(item.cancel_url)}" data-kind="${item.kind}" class="secondary">${t('cancel')}</button>`:''}
  </article>`;
}
async function loadActivity(quiet=false){
  try{
    const data=await api('/api/active-processes'),active=data.count>0;
    $('#backup-activity-button').classList.toggle('hidden',!active);
    $('#activity-summary').textContent=active?`${data.count} ${t('runningState').toLowerCase()}`:t('activeProcesses');
    $('#activity-content').innerHTML=data.items.length?data.items.map(processCard).join(''):`<div class="empty-list"><p>${t('noActiveProcesses')}</p></div>`;
    applyProgressWidths($('#activity-content'));
  }catch(error){if(!quiet)toast(error.message,'error');}
}
$('#backup-activity-button').addEventListener('click',()=>{$('#activity-dialog').showModal();loadActivity();});
$('#activity-content').addEventListener('click',async event=>{
  const button=event.target.closest('[data-cancel-process]');if(!button)return;
  const confirmText=button.dataset.kind==='transfer'?t('confirmCancelRestoreTitle'):t('confirmCancelBackupTitle2');
  if(!await confirmAction(confirmText,t('confirmCancelProcessCopy')))return;
  try{await api(button.dataset.cancelProcess,{method:'POST'});toast(t('toastCancelRequested'));await loadActivity();}catch(error){toast(error.message,'error');}
});
const collapsed=localStorage.getItem('emboxa-sidebar-collapsed')==='true';document.body.classList.toggle('sidebar-collapsed',collapsed);
$('#sidebar-collapse').addEventListener('click',()=>{document.body.classList.toggle('sidebar-collapsed');localStorage.setItem('emboxa-sidebar-collapsed',document.body.classList.contains('sidebar-collapsed'));});

function importProgress(status, percent = 0, detail = '') {
  $('#import-status').textContent = status;
  $('#import-percent').textContent = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
  $('#import-progress-bar').style.width = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
  $('#import-detail').textContent = detail;
}
function uploadArchiveFormData(data, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const started = performance.now();
    xhr.open('POST', '/api/import');
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = event => {
      if (!event.lengthComputable) return onProgress?.(8, t('uploadInProgress'));
      const percent = Math.max(1, Math.min(70, Math.round((event.loaded / event.total) * 70)));
      const seconds = Math.max(.1, (performance.now() - started) / 1000);
      onProgress?.(percent, `${t('uploadingArchive')} · ${bytes(event.loaded)} / ${bytes(event.total)} · ${bytes(event.loaded / seconds)}/s`);
    };
    xhr.upload.onload = () => onProgress?.(72, t('uploadCompleteVerifying'));
    xhr.onload = () => {
      let payload = null;
      try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch {}
      if (xhr.status === 401) { location.href = '/login'; return reject(new Error(t('sessionExpired'))); }
      if (xhr.status < 200 || xhr.status >= 300) return reject(new Error(payload?.detail || `${t('errorPrefix')} ${xhr.status}`));
      resolve(payload);
    };
    xhr.onerror = () => reject(new Error(t('importFailedGeneric')));
    xhr.send(data);
  });
}
async function waitForArchiveImportJob(job) {
  let current = job;
  while (current.status === 'queued' || current.status === 'running') {
    importProgress(current.status === 'queued' ? t('importQueued') : t('importRunning'), current.percent || 1, current.detail || t('importBackgroundNote'));
    await wait(1500);
    current = await api(current.status_url);
  }
  if (current.status === 'failed') throw new Error(current.error || current.detail || t('importFailedGeneric'));
  return current;
}
function ioCard(action, svg, title, copy) {
  return `<button class="io-card" type="button" data-io="${action}">${svg}<b>${esc(title)}</b><small>${esc(copy)}</small></button>`;
}
function setIO(title, subtitle, html) {
  // title/subtitle are already-localized strings coming from t().
  $('#io-title').textContent = title;
  $('#io-subtitle').textContent = subtitle;
  $('#io-content').innerHTML = html;
}
function renderIOMain() {
  setIO(t('navImportExport'), t('ioMainSubtitle'), `<div class="io-grid">
    ${ioCard('import-mailvault', icon('upload'), t('importMailvaultTitle'), t('importMailvaultCopy'))}
    ${ioCard('import-mbox', icon('mail'), t('importMboxTitle'), t('importMboxCopy'))}
    ${ioCard('export-start', icon('archive'), t('exportArchiveTitle'), t('exportArchiveCopy2'))}
  </div>`);
}
function renderMailvaultImportMethods() {
  setIO(t('importMailvaultTitle'), t('chooseSourceTitle'), `<button class="ghost io-back" type="button" data-io="back">${t('backLink')}</button><div class="io-grid">
    ${ioCard('mailvault-upload', icon('upload'), t('uploadTitle'), t('uploadCopy'))}
    ${ioCard('mailvault-link', icon('link'), t('linkTitle'), t('linkCopyMailvault'))}
    ${ioCard('mailvault-local', icon('server'), t('nasFolderTitle'), t('nasFolderCopyMailvault'))}
  </div>`);
}
function renderMboxImportMethods() {
  setIO(t('importMboxTitle'), t('chooseMboxImportTitle'), `<button class="ghost io-back" type="button" data-io="back">${t('backLink')}</button><div class="io-grid">
    ${ioCard('mbox-upload', icon('upload'), t('uploadFolderFileTitle'), t('uploadFolderFileCopy'))}
    ${ioCard('mbox-link', icon('link'), t('linkTitle'), t('linkCopyMbox'))}
    ${ioCard('mbox-local', icon('server'), t('nasFolderTitle'), t('nasFolderCopyMbox'))}
  </div>`);
}
function renderExportAccounts() {
  const accounts = state.accounts.filter(account => account.has_archive);
  setIO(t('exportArchiveTitle'), t('chooseExportMailboxTitle'), `<button class="ghost io-back" type="button" data-io="back">${t('backLink')}</button><div class="io-grid">${
    accounts.length ? accounts.map(account => ioCard(`export-account:${account.id}`, icon('archive'), account.display_name, `${account.email} · ${bytes(account.archive_size)}`)).join('') : `<p class="muted">${t('noExportableArchive')}</p>`
  }</div>`);
}
function renderExportMethods(accountId) {
  const account = state.accounts.find(item => item.id === accountId);
  setIO(t('exportArchiveTitle'), account ? `${account.display_name} · ${account.email}` : t('chooseDestination'), `<button class="ghost io-back" type="button" data-io="export-start">${t('backToMailboxes')}</button><div class="io-grid">
    ${ioCard(`export-browser:${accountId}`, icon('upload'), t('downloadBrowserTitle'), t('downloadBrowserCopy'))}
    ${ioCard(`export-nas:${accountId}`, icon('server'), t('nasFolderTitle'), t('nasFolderExportCopy'))}
  </div>`);
}
$('#import-export-button').addEventListener('click',()=>{renderIOMain();$('#io-dialog').showModal();$('.main-sidebar').classList.remove('open');});
$('#io-content').addEventListener('click',event=>{
  const button=event.target.closest('[data-io]'); if(!button)return;
  const action=button.dataset.io;
  if(action==='back')return renderIOMain();
  if(action==='import-mailvault')return renderMailvaultImportMethods();
  if(action==='import-mbox')return renderMboxImportMethods();
  if(action==='export-start')return renderExportAccounts();
  if(action.startsWith('export-account:'))return renderExportMethods(Number(action.split(':')[1]));
  $('#io-dialog').close();
  if(action==='mailvault-upload')return $('#import-input').click();
  if(action==='mailvault-link')return importArchiveFromLink();
  if(action==='mailvault-local')return importArchiveFromNas('mailvault');
  if(action==='mbox-upload')return $('#mbox-import-input').click();
  if(action==='mbox-link')return importMboxFromLink();
  if(action==='mbox-local')return importArchiveFromNas('mbox');
  if(action.startsWith('export-browser:'))return exportArchive(Number(action.split(':')[1]));
  if(action.startsWith('export-nas:'))return exportArchiveToNas(Number(action.split(':')[1]));
});
$('#import-input').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;
  const data=new FormData();data.append('file',file);
  const dialog=$('#import-dialog');$('#import-close').disabled=true;$('#import-done').disabled=true;
  $('#import-summary').textContent=`${t('localFileSummary')} · ${esc(file.name)} · ${bytes(file.size)}`;
  importProgress(t('uploadingArchive'),1,t('uploadingFileNote'));
  dialog.showModal();
  try{
    const job=await uploadArchiveFormData(data,(percent,detail)=>importProgress(t('uploadingArchive'),percent,detail));
    await waitForArchiveImportJob(job);
    importProgress(t('importCompleted'),100,t('importCompletedNote'));
    toast(t('toastArchiveImported'));
    loadAccounts();
  }catch(error){importProgress(t('importNotCompleted'),0,error.message);toast(error.message,'error');}
  finally{$('#import-close').disabled=false;$('#import-done').disabled=false;event.target.value='';}
});
async function importArchiveFromLink(){
  const url=(prompt(t('promptMailvaultLink'))||'').trim();
  if(!url)return;
  const dialog=$('#import-dialog');$('#import-close').disabled=true;$('#import-done').disabled=true;
  $('#import-summary').textContent=t('importFromLinkSummary');
  importProgress(t('downloadingArchive'),1,t('downloadingDirectNote'));
  dialog.showModal();
  try{
    const job=await api('/api/import/link',{method:'POST',body:JSON.stringify({url})});
    await waitForArchiveImportJob(job);
    importProgress(t('importCompleted'),100,t('importCompletedNote'));
    toast(t('toastArchiveImported'));
    loadAccounts();
  }catch(error){importProgress(t('importNotCompleted'),0,error.message);toast(error.message,'error');}
  finally{$('#import-close').disabled=false;$('#import-done').disabled=false;}
}
async function importArchiveFromNas(mode='auto'){
  if(!['auto','mailvault','mbox'].includes(mode))return toast(t('invalidChoice'),'error');
  const dialog=$('#import-dialog');$('#import-close').disabled=true;$('#import-done').disabled=true;
  $('#import-summary').textContent=t('importFromNasSummary');
  importProgress(t('importingFromNas'),1,t('scanningLocalFolder'));
  dialog.showModal();
  try{
    const job=await api('/api/import/local',{method:'POST',body:JSON.stringify({mode,display_name:'Import NAS',email:'nas-import@local.invalid'})});
    const result=await waitForArchiveImportJob(job);
    importProgress(t('importCompleted'),100,`${result.account_ids?.length||1} ${t('createdArchivesFromNas')}`);
    toast(t('toastNasImportCompleted'));
    loadAccounts();
  }catch(error){importProgress(t('importNotCompleted'),0,error.message);toast(error.message,'error');}
  finally{$('#import-close').disabled=false;$('#import-done').disabled=false;}
}
function mboxImportProgress(status, percent = 0, detail = '') {
  $('#mbox-import-status').textContent = status;
  $('#mbox-import-percent').textContent = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
  $('#mbox-import-progress-bar').style.width = `${Math.max(0, Math.min(100, Math.round(percent)))}%`;
  $('#mbox-import-detail').textContent = detail;
}
async function waitForMboxImportJob(job) {
  let current = job;
  while (current.status === 'queued' || current.status === 'running') {
    mboxImportProgress(current.status === 'queued' ? t('mboxImportQueued') : t('mboxImportRunning'), current.percent || 10, current.detail || t('mboxParsingBackground'));
    await wait(1500);
    current = await api(current.status_url);
  }
  if (current.status === 'failed') throw new Error(current.error || current.detail || t('mboxImportFailedGeneric'));
  return current.account;
}
function uploadMboxFormData(data, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/import/mbox');
    xhr.setRequestHeader('X-CSRF-Token', csrf);
    xhr.upload.onprogress = event => {
      if (!event.lengthComputable) return onProgress?.(8, t('uploadMboxInProgress'));
      const percent = Math.max(1, Math.min(32, Math.round((event.loaded / event.total) * 32)));
      onProgress?.(percent, `${t('uploadingMbox')} · ${bytes(event.loaded)} / ${bytes(event.total)}`);
    };
    xhr.onload = () => {
      let payload = null;
      try { payload = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch {}
      if (xhr.status === 401) { location.href = '/login'; return reject(new Error(t('sessionExpired'))); }
      if (xhr.status < 200 || xhr.status >= 300) return reject(new Error(payload?.detail || `${t('errorPrefix')} ${xhr.status}`));
      resolve(payload);
    };
    xhr.onerror = () => reject(new Error(t('mboxImportFailedGeneric')));
    xhr.send(data);
  });
}
$('#mbox-import-input').addEventListener('change',async event=>{
  const files=[...event.target.files]; if(!files.length)return;
  const defaultName=files[0].webkitRelativePath?.split('/')[0]||files[0].name?.replace(/\.mbox$/i,'')||t('defaultMboxArchiveName');
  const displayName=(prompt(t('promptMboxDisplayName'), defaultName)||defaultName).trim();
  const data=new FormData(); data.append('display_name',displayName); data.append('email','mbox-import@local.invalid');
  files.forEach(file=>data.append('files',file,file.webkitRelativePath||file.name));
  const dialog=$('#mbox-import-dialog'); $('#mbox-import-close').disabled=true; $('#mbox-import-done').disabled=true;
  $('#mbox-import-summary').textContent=`${files.length} ${t('filesSelectedSummary')}`;
  mboxImportProgress(t('uploadingMbox'),5,t('uploadingMboxNote'));
  dialog.showModal();
  try{
    const job=await uploadMboxFormData(data,(percent,detail)=>mboxImportProgress(t('uploadingMbox'),percent,detail));
    const account=await waitForMboxImportJob(job);
    mboxImportProgress(t('mboxImportCompleted'),100,`${numberFmt(account?.message_count||0)} ${t('mboxImportCompletedNote')}`);
    toast(t('toastMboxImportCompleted'));
    loadAccounts();
  }catch(error){mboxImportProgress(t('mboxImportNotCompleted'),0,error.message);toast(error.message,'error');}
  finally{$('#mbox-import-close').disabled=false;$('#mbox-import-done').disabled=false;event.target.value='';}
});
async function importMboxFromLink(){
  const url=(prompt(t('promptMboxLink'))||'').trim();
  if(!url)return;
  const defaultName=new URL(url, location.href).pathname.split('/').pop()?.replace(/\.mbox$/i,'')||t('defaultMboxArchiveName');
  const displayName=(prompt(t('promptMboxDisplayName'), defaultName)||defaultName).trim();
  const dialog=$('#mbox-import-dialog'); $('#mbox-import-close').disabled=true; $('#mbox-import-done').disabled=true;
  $('#mbox-import-summary').textContent=t('mboxImportFromLinkSummary');
  mboxImportProgress(t('downloadingMbox'),1,t('downloadingDirectNote'));
  dialog.showModal();
  try{
    const job=await api('/api/import/mbox/link',{method:'POST',body:JSON.stringify({url,display_name:displayName,email:'mbox-import@local.invalid'})});
    const account=await waitForMboxImportJob(job);
    mboxImportProgress(t('mboxImportCompleted'),100,`${numberFmt(account?.message_count||0)} ${t('mboxImportCompletedNote')}`);
    toast(t('toastMboxImportCompleted'));
    loadAccounts();
  }catch(error){mboxImportProgress(t('mboxImportNotCompleted'),0,error.message);toast(error.message,'error');}
  finally{$('#mbox-import-close').disabled=false;$('#mbox-import-done').disabled=false;}
}
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
/* apply() resolves 'auto' to it/en and returns that resolved value; the two pill selects always
   show a real language, never the internal 'auto' sentinel (which isn't one of their options). */
function localize(){
  const resolved=EMBOXA_I18N.apply(preferences.locale);
  $('#language-select').value=resolved;$('#settings-language').value=resolved;
  EMBOXA_I18N.syncLanguageMenus?.();
  refreshLocalizedViews();
}
/* Static markup re-translates itself via data-i18n; content already rendered into innerHTML from
   JS template literals does not, so a locale switch re-runs whatever produced the current view. */
function refreshLocalizedViews(){
  if (typeof loadWebUsage === 'function') loadWebUsage();
  if (typeof renderTelegramPrefs === 'function') renderTelegramPrefs();
  if (state.account) {
    renderVersions();
    renderFolders();
    if (state.archiveView === 'attachments') loadAttachments(); else loadMessages();
  } else if (state.accounts.length) {
    renderAccounts();
  }
  if ($('#activity-dialog').open) loadActivity(true);
}
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
const microsoftResult = new URLSearchParams(location.search).get('microsoft');
if (microsoftResult === 'connected') { toast(t('toastMicrosoftConnected')); history.replaceState({}, '', '/app'); }
if (microsoftResult === 'error') { toast(new URLSearchParams(location.search).get('reason') || t('microsoftOauthFailedGeneric'), 'error'); history.replaceState({}, '', '/app'); }
state.polling=setInterval(()=>{loadActivity(true);loadAccounts(true);},2500);
