const consentKey='emboxa-cookie-consent-v1';
const banner=document.querySelector('#cookie-banner');
function loadAnalytics(){const id=document.querySelector('meta[name="ga-id"]')?.content;if(!id||!/^G-[A-Z0-9]+$/i.test(id)||window.gtag)return;window.dataLayer=window.dataLayer||[];window.gtag=function(){dataLayer.push(arguments)};gtag('js',new Date());gtag('config',id,{send_page_view:true,allow_google_signals:false});const script=document.createElement('script');script.async=true;script.src=`https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;document.head.append(script);}
function applyConsent(value){localStorage.setItem(consentKey,value);banner?.classList.add('hidden');if(value==='analytics')loadAnalytics();}
document.querySelectorAll('[data-consent]').forEach(button=>button.addEventListener('click',()=>applyConsent(button.dataset.consent)));
document.querySelector('#cookie-settings')?.addEventListener('click',()=>banner.classList.remove('hidden'));
const stored=localStorage.getItem(consentKey);if(!stored)banner?.classList.remove('hidden');else if(stored==='analytics')loadAnalytics();
window.emboxaTrack=name=>{if(localStorage.getItem(consentKey)==='analytics'&&window.gtag&&['registration_completed','login_completed','mailbox_added','backup_started','backup_completed','export_created'].includes(name))gtag('event',name);};
