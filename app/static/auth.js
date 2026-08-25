const form = document.querySelector('#auth-form');
const errorBox = document.querySelector('#form-error');
const savedTheme = localStorage.getItem('emboxa-theme') || localStorage.getItem('mailvault-theme') || 'light';
document.documentElement.dataset.theme = savedTheme === 'system'
  ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  : savedTheme;
const authLanguage=document.querySelector('#auth-language');if(authLanguage){authLanguage.value=localStorage.getItem('emboxa-locale')||'auto';EMBOXA_I18N.apply(authLanguage.value);authLanguage.addEventListener('change',()=>{localStorage.setItem('emboxa-locale',authLanguage.value);EMBOXA_I18N.apply(authLanguage.value);});}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form));
  if (form.dataset.mode === 'setup' && data.password !== data.confirm) {
    errorBox.textContent = 'Le password non coincidono.';
    return;
  }
  delete data.confirm;
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  errorBox.textContent = '';
  try {
    const response = await fetch(`/api/${form.dataset.mode}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Operazione non riuscita');
    location.href = result.next || '/app';
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
