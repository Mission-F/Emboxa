const consentKey='emboxa-cookie-consent-v1';
const banner=document.querySelector('#cookie-banner');
function loadAnalytics(){const id=document.querySelector('meta[name="ga-id"]')?.content;if(!id||!/^G-[A-Z0-9]+$/i.test(id)||window.gtag)return;window.dataLayer=window.dataLayer||[];window.gtag=function(){dataLayer.push(arguments)};gtag('js',new Date());gtag('config',id,{send_page_view:true,allow_google_signals:false});const script=document.createElement('script');script.async=true;script.src=`https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;document.head.append(script);}
function applyConsent(value){localStorage.setItem(consentKey,value);banner?.classList.add('hidden');if(value==='analytics')loadAnalytics();}
document.querySelectorAll('[data-consent]').forEach(button=>button.addEventListener('click',()=>applyConsent(button.dataset.consent)));
document.querySelector('#cookie-settings')?.addEventListener('click',()=>banner.classList.remove('hidden'));
const stored=localStorage.getItem(consentKey);if(!stored)banner?.classList.remove('hidden');else if(stored==='analytics')loadAnalytics();
window.emboxaTrack=name=>{if(localStorage.getItem(consentKey)==='analytics'&&window.gtag&&['registration_completed','login_completed','mailbox_added','backup_started','backup_completed','export_created'].includes(name))gtag('event',name);};

const revealNodes=[...document.querySelectorAll('.reveal, .reveal-3d')];
const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
if(reducedMotion||!('IntersectionObserver' in window)) revealNodes.forEach(node=>node.classList.add('is-visible'));
else {
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(!entry.isIntersecting)return;
    entry.target.classList.add('is-visible');
    observer.unobserve(entry.target);
  }),{rootMargin:'0px 0px -8% 0px',threshold:.12});
  revealNodes.forEach(node=>observer.observe(node));
}

const mobileMenu=document.querySelector('.mobile-menu');
mobileMenu?.querySelectorAll('a').forEach(link=>link.addEventListener('click',()=>{mobileMenu.open=false;}));
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&mobileMenu?.open){mobileMenu.open=false;mobileMenu.querySelector('summary')?.focus();}});

// Motion layer: scroll progress bar, hero parallax, cursor spotlight and 3D card tilt.
// Every effect writes only CSS custom properties (--scroll, --hero-scroll, --mx, --my, --rx, --ry);
// the actual transforms live in public.css so prefers-reduced-motion can neutralize them from one place.
if(!reducedMotion){
  const root=document.documentElement;
  const hero=document.querySelector('.hero');
  let ticking=false;
  function onScroll(){
    const doc=document.documentElement;
    const pageMax=doc.scrollHeight-doc.clientHeight;
    root.style.setProperty('--scroll', pageMax>0?Math.min(1,doc.scrollTop||window.scrollY)/pageMax:0);
    const heroMax=Math.max(1,window.innerHeight*.9);
    root.style.setProperty('--hero-scroll', Math.max(0,Math.min(1,(window.scrollY)/heroMax)));
    ticking=false;
  }
  window.addEventListener('scroll',()=>{if(!ticking){requestAnimationFrame(onScroll);ticking=true;}},{passive:true});
  onScroll();

  hero?.addEventListener('mousemove',event=>{
    const rect=hero.getBoundingClientRect();
    hero.style.setProperty('--mx', `${((event.clientX-rect.left)/rect.width*100).toFixed(2)}%`);
    hero.style.setProperty('--my', `${((event.clientY-rect.top)/rect.height*100).toFixed(2)}%`);
  });

  const tiltEls=[...document.querySelectorAll('.plan-card, .demo-grid article')];
  tiltEls.forEach(el=>{
    el.classList.add('tilt');
    el.addEventListener('mousemove',event=>{
      const rect=el.getBoundingClientRect();
      const px=(event.clientX-rect.left)/rect.width-.5;
      const py=(event.clientY-rect.top)/rect.height-.5;
      el.style.setProperty('--rx', `${(px*9).toFixed(2)}deg`);
      el.style.setProperty('--ry', `${(py*-9).toFixed(2)}deg`);
    });
    el.addEventListener('mouseleave',()=>{el.style.setProperty('--rx','0deg');el.style.setProperty('--ry','0deg');});
  });
}
