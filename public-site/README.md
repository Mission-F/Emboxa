# Emboxa public site

Static Cloudflare Pages build for `https://emboxa.eu`.

```bash
python public-site/build.py
npx wrangler pages deploy public-site/dist --project-name emboxa --branch main
```

Application CTAs intentionally point to `https://app.emboxa.eu`.
