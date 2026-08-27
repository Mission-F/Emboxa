const authForm = document.querySelector('form');
const authStatus = document.querySelector('.form-status');
const authLocale = document.querySelector('.auth-locale');
const localePreference = localStorage.getItem('emboxa-locale') || 'auto';

/* apply() resolves 'auto' to it/en and returns that resolved value; feed it back into the select
   so the IT/EN pill highlights correctly (the select carries no 'auto' option). */
function applyAuthLocale(preference) {
  const locale = window.EMBOXA_I18N?.apply(preference) || 'en';
  if (authLocale) authLocale.value = locale;
  window.EMBOXA_I18N?.syncLanguageMenus?.();
  const intro = document.querySelector('.auth-form-wrap > p.muted');
  if (intro && document.querySelector('#login-form')) intro.textContent = window.EMBOXA_I18N.t('loginIntro', locale);
  return locale;
}
applyAuthLocale(localePreference);
if (authLocale) {
  authLocale.addEventListener('change', () => { localStorage.setItem('emboxa-locale', authLocale.value); applyAuthLocale(authLocale.value); });
}

const systemDark = matchMedia('(prefers-color-scheme: dark)');
const applyTheme = () => {
  const saved = localStorage.getItem('emboxa-theme') || 'system';
  document.documentElement.dataset.theme = saved === 'system' ? (systemDark.matches ? 'dark' : 'light') : saved;
};
applyTheme();
systemDark.addEventListener?.('change', applyTheme);

const translatedErrors = {
  it: {
    email: 'Inserisci un indirizzo email valido.', required: 'Questo campo è obbligatorio.', password: 'Usa almeno 10 caratteri.', mismatch: 'Le password non coincidono.', terms: 'Accetta Termini e Privacy per continuare.', invalid: 'Email o password non valide.', verify: 'Verifica la tua email prima di accedere.', expired: 'Il codice o il link non è valido oppure è scaduto.', exists: 'Esiste già un account con questa email.', generic: 'Non è stato possibile completare la richiesta.', sent: 'Se l’account esiste, abbiamo inviato il link di recupero.', resent: 'Nuovo codice inviato.', updated: 'Password aggiornata. Ora puoi accedere.'
  },
  en: {
    email: 'Enter a valid email address.', required: 'This field is required.', password: 'Use at least 10 characters.', mismatch: 'Passwords do not match.', terms: 'Accept the Terms and Privacy Policy to continue.', invalid: 'Email or password is invalid.', verify: 'Verify your email before signing in.', expired: 'The code or link is invalid or has expired.', exists: 'An account already exists for this email.', generic: 'The request could not be completed.', sent: 'If the account exists, a reset link has been sent.', resent: 'A new code was sent.', updated: 'Password updated. You can now log in.'
  }
};
const locale = () => window.EMBOXA_I18N?.resolve(authLocale?.value || localePreference) || 'en';
const errorText = key => translatedErrors[locale()]?.[key] || translatedErrors.en[key];

function clearErrors() {
  if (authStatus) { authStatus.textContent = ''; authStatus.classList.remove('success'); }
  document.querySelectorAll('.field-error').forEach(node => { node.textContent = ''; });
  document.querySelectorAll('[aria-invalid=true]').forEach(node => node.removeAttribute('aria-invalid'));
}
function setFieldError(name, message) {
  const input = authForm?.elements[name];
  const target = document.querySelector(`[data-error-for="${name}"]`);
  if (input) input.setAttribute('aria-invalid', 'true');
  if (target) target.textContent = message;
  else if (authStatus) authStatus.textContent = message;
}
function validEmail(value) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value); }

async function send(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : '';
    const lower = detail.toLowerCase();
    let key = 'generic';
    if (lower.includes('credentials')) key = 'invalid';
    else if (lower.includes('verify')) key = 'verify';
    else if (lower.includes('expired') || lower.includes('invalid')) key = 'expired';
    else if (lower.includes('already exists')) key = 'exists';
    else if (lower.includes('disabled')) key = 'generic';
    const error = new Error(errorText(key));
    error.detail = detail;
    error.status = response.status;
    throw error;
  }
  return data;
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
function authenticationOptionsFromJSON(options) {
  const publicKey = {...options, challenge: b64urlToBuffer(options.challenge)};
  if (publicKey.allowCredentials) {
    publicKey.allowCredentials = publicKey.allowCredentials.map(item => ({...item, id: b64urlToBuffer(item.id)}));
  }
  return publicKey;
}

function setLoading(button, loading, text) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = loading;
  button.textContent = loading ? text : button.dataset.label;
}

document.querySelectorAll('[data-password-toggle]').forEach(button => button.addEventListener('click', () => {
  const input = button.parentElement.querySelector('input');
  input.type = input.type === 'password' ? 'text' : 'password';
  button.textContent = window.EMBOXA_I18N.t(input.type === 'password' ? 'show' : 'hide', authLocale?.value || localePreference);
  button.setAttribute('aria-label', button.textContent);
  input.focus({preventScroll: true});
}));

document.querySelectorAll('input').forEach(input => input.addEventListener('input', () => {
  input.removeAttribute('aria-invalid');
  const target = document.querySelector(`[data-error-for="${input.name}"]`);
  if (target) target.textContent = '';
}));

const verifyEmail = document.querySelector('#verify-form [name=email]');
if (verifyEmail) {
  verifyEmail.value = new URLSearchParams(location.search).get('email') || sessionStorage.getItem('emboxa-verify-email') || '';
  document.querySelector('#verify-destination').textContent = verifyEmail.value;
  verifyEmail.addEventListener('input', () => { document.querySelector('#verify-destination').textContent = verifyEmail.value; });
}
const otp = document.querySelector('.otp-input');
otp?.addEventListener('input', () => { otp.value = otp.value.replace(/\D/g, '').slice(0, 6); });

function startResendCooldown(button, seconds = 60) {
  if (!button) return;
  let remaining = seconds;
  button.disabled = true;
  const base = window.EMBOXA_I18N.t('resendCode', authLocale?.value || localePreference);
  button.textContent = `${base} (${remaining}s)`;
  clearInterval(startResendCooldown.timer);
  startResendCooldown.timer = setInterval(() => {
    remaining -= 1;
    button.textContent = `${base} (${remaining}s)`;
    if (remaining <= 0) { clearInterval(startResendCooldown.timer); button.disabled = false; button.textContent = base; }
  }, 1000);
}

const resetToken = new URLSearchParams(location.search).get('token');
if (authForm?.id === 'reset-form' && resetToken) {
  document.querySelector('.reset-request').classList.add('hidden');
  document.querySelectorAll('.reset-confirm').forEach(node => node.classList.remove('hidden'));
  authForm.email.required = false;
  authForm.password.required = true;
  authForm.confirm_password.required = true;
  authForm.querySelector('[type=submit]').textContent = window.EMBOXA_I18N.t('setNewPassword', authLocale?.value || localePreference);
}

const passkeyLogin = document.querySelector('#passkey-login');
passkeyLogin?.addEventListener('click', async () => {
  clearErrors();
  if (!window.PublicKeyCredential || !navigator.credentials) {
    authStatus.textContent = locale() === 'it' ? 'Questo browser non supporta le passkey.' : 'This browser does not support passkeys.';
    return;
  }
  const email = authForm?.elements.username?.value?.trim();
  setLoading(passkeyLogin, true, 'Passkey…');
  try {
    const options = await send('/api/passkeys/authentication/options', {email: email || null});
    const credential = await navigator.credentials.get({publicKey: authenticationOptionsFromJSON(options)});
    if (!credential) throw new Error(locale() === 'it' ? 'Passkey annullata.' : 'Passkey cancelled.');
    await send('/api/passkeys/authentication/verify', {credential: credentialToJSON(credential)});
    window.emboxaTrack?.('passkey_login_completed');
    location.href = '/app';
  } catch (error) {
    authStatus.textContent = error.message || errorText('generic');
  } finally {
    setLoading(passkeyLogin, false);
  }
});

authForm?.addEventListener('submit', async event => {
  event.preventDefault();
  clearErrors();
  const values = Object.fromEntries(new FormData(authForm));
  let valid = true;
  const email = (values.email || values.username || '').trim();
  if ((authForm.id !== 'reset-form' || !resetToken) && !validEmail(email)) { setFieldError(values.username !== undefined ? 'username' : 'email', errorText('email')); valid = false; }
  if (values.password !== undefined && values.password.length < (authForm.id === 'login-form' ? 1 : 10)) { setFieldError('password', errorText(authForm.id === 'login-form' ? 'required' : 'password')); valid = false; }
  if (values.confirm_password !== undefined && values.password !== values.confirm_password) { setFieldError('confirm_password', errorText('mismatch')); valid = false; }
  if (authForm.id === 'verify-form' && !/^\d{6}$/.test(values.code || '')) { setFieldError('code', errorText('expired')); valid = false; }
  if (authForm.id === 'register-form' && !authForm.terms.checked) { authStatus.textContent = errorText('terms'); valid = false; }
  if (!valid) return;

  const button = authForm.querySelector('[type=submit]');
  const loading = authForm.id === 'login-form' ? (locale() === 'it' ? 'Accesso…' : 'Logging in…') : (locale() === 'it' ? 'Invio…' : 'Sending…');
  setLoading(button, true, loading);
  try {
    if (authForm.id === 'login-form') {
      await send('/api/login', {username: email, password: values.password});
      window.emboxaTrack?.('login_completed');
      location.href = '/app';
      return;
    }
    if (authForm.id === 'register-form') {
      await send('/api/register', {email, password: values.password, locale: locale()});
      sessionStorage.setItem('emboxa-verify-email', email);
      window.emboxaTrack?.('registration_completed');
      location.href = `/verify?email=${encodeURIComponent(email)}`;
      return;
    }
    if (authForm.id === 'verify-form') {
      const result = await send('/api/verify', {email, code: values.code});
      sessionStorage.removeItem('emboxa-verify-email');
      location.href = result.next || '/app';
      return;
    }
    if (authForm.id === 'reset-form' && resetToken) {
      await send('/api/password-reset/confirm', {token: resetToken, password: values.password});
      authStatus.textContent = errorText('updated');
      authStatus.classList.add('success');
      button.classList.add('hidden');
      return;
    }
    if (authForm.id === 'reset-form') {
      await send('/api/password-reset/request', {email});
      authStatus.textContent = errorText('sent');
      authStatus.classList.add('success');
      button.disabled = true;
      return;
    }
  } catch (error) {
    authStatus.textContent = error.message;
    if (authForm.id === 'login-form' && error.status === 403 && error.detail?.toLowerCase().includes('verify')) {
      sessionStorage.setItem('emboxa-verify-email', email);
      const link = document.createElement('a'); link.href = `/verify?email=${encodeURIComponent(email)}`; link.textContent = ` ${window.EMBOXA_I18N.t('verify', authLocale?.value || localePreference)}`; authStatus.append(link);
    }
  } finally { if (!button.classList.contains('hidden') && !authStatus?.classList.contains('success')) setLoading(button, false); }
});

const resend = document.querySelector('#resend-code');
if (resend && verifyEmail?.value) startResendCooldown(resend);
resend?.addEventListener('click', async () => {
  clearErrors();
  const email = verifyEmail.value.trim();
  if (!validEmail(email)) { setFieldError('email', errorText('email')); return; }
  try {
    await send('/api/verification/resend', {email});
    authStatus.textContent = errorText('resent');
    authStatus.classList.add('success');
    startResendCooldown(resend);
  } catch (error) { authStatus.textContent = error.message; }
});
