from __future__ import annotations

import os
import shutil
import sys
import ast
from html import escape
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import (
    GITHUB_REPOSITORY_URL,
    LEGAL_ADDRESS,
    LEGAL_CONTACT_EMAIL,
    LEGAL_ENTITY_NAME,
    LEGAL_VAT_ID,
    PUBLIC_APP_URL,
    PUBLIC_SITE_URL,
    STANDARD_MAILBOX_LIMIT,
    STANDARD_RETENTION_DAYS,
    STANDARD_STORAGE_LIMIT_BYTES,
)
DIST = ROOT / "public-site" / "dist"
PUBLIC_URL = os.getenv("PUBLIC_SITE_URL", PUBLIC_SITE_URL).rstrip("/")
APP_URL = os.getenv("PUBLIC_APP_URL", PUBLIC_APP_URL).rstrip("/")
GITHUB_URL = os.getenv("GITHUB_REPOSITORY_URL", GITHUB_REPOSITORY_URL).rstrip("/")
STATIC_DIR = ROOT / "app" / "static"


def load_main_constant(name: str):
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise RuntimeError(f"Missing {name} in app/main.py")


PUBLIC_PAGES = load_main_constant("PUBLIC_PAGES")
SEO_PAGES = load_main_constant("SEO_PAGES")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def page_path(locale: str, page: str) -> Path:
    return DIST / locale / ("index.html" if page == "home" else f"{page}/index.html")


def seo(page: str, locale: str) -> tuple[str, str, str]:
    fallback = "Versioned IMAP email backup, searchable email archive and IMAP Transfer restore."
    return SEO_PAGES.get(page, {}).get(locale, (
        f"{page.replace('-', ' ').title()} — Emboxa Web",
        fallback,
        "IMAP backup, email archive",
    ))


def render_pages() -> None:
    env = Environment(
        loader=FileSystemLoader(ROOT / "app" / "templates"),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("public.html")
    pages = ["home", *sorted(PUBLIC_PAGES)]
    for locale in ("it", "en"):
        for page in pages:
            title, description, keywords = seo(page, locale)
            path = "" if page == "home" else page
            html = template.render(
                locale=locale,
                page=page,
                canonical=f"{PUBLIC_URL}/{locale}/" + path,
                public_url=PUBLIC_URL,
                app_url=APP_URL,
                app_name="Emboxa Web",
                analytics_id=os.getenv("GOOGLE_ANALYTICS_ID", ""),
                from_email=os.getenv("LEGAL_CONTACT_EMAIL", LEGAL_CONTACT_EMAIL),
                github_url=GITHUB_URL,
                logged_in=False,
                retention_days=int(os.getenv("STANDARD_RETENTION_DAYS", str(STANDARD_RETENTION_DAYS))),
                storage_limit_gb=round(int(os.getenv("STANDARD_STORAGE_LIMIT_BYTES", str(STANDARD_STORAGE_LIMIT_BYTES))) / 1024**3),
                mailbox_limit=int(os.getenv("STANDARD_MAILBOX_LIMIT", str(STANDARD_MAILBOX_LIMIT))),
                version_limit=int(os.getenv("DEFAULT_BACKUP_RETENTION_VERSIONS", "3")),
                permanent_limit=int(os.getenv("PERMANENT_MAILBOX_LIMIT", "1")),
                legal_entity=os.getenv("LEGAL_ENTITY_NAME", LEGAL_ENTITY_NAME),
                legal_address=os.getenv("LEGAL_ADDRESS", LEGAL_ADDRESS),
                legal_vat=os.getenv("LEGAL_VAT_ID", LEGAL_VAT_ID),
                legal_email=os.getenv("LEGAL_CONTACT_EMAIL", LEGAL_CONTACT_EMAIL),
                seo_title=title,
                seo_description=description,
                seo_keywords=keywords,
            )
            write(page_path(locale, page), html)


def copy_assets() -> None:
    target = DIST / "static"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("public.css", "public.js", "manifest.webmanifest", "emboxa-home-visual.png"):
        shutil.copy2(STATIC_DIR / name, target / name)
    shutil.copytree(STATIC_DIR / "icons", target / "icons", dirs_exist_ok=True)


def locale_redirect_page(target: str, label: str) -> str:
    """A tiny redirect page: picks it/en from the browser language, then falls back to a meta-refresh
    for clients without JS. Used for every bare, locale-less path (e.g. /privacy) so it responds with
    real content instead of a 404, matching how the homepage already redirects '/' to '/en/'."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Emboxa Web</title>'
        f"<script>const lang=(navigator.language||'en').toLowerCase().startsWith('it')?'it':'en';"
        f"location.replace('/'+lang+'/{target}');</script>"
        f'<meta http-equiv="refresh" content="0; url=/en/{target}"></head>'
        f'<body><p><a href="/en/{target}">{label}</a></p></body></html>'
    )


def render_index() -> None:
    write(DIST / "index.html", locale_redirect_page("", "Continue to Emboxa Web"))


def render_bare_pages() -> None:
    """Every public page also responds at its bare, locale-less path (emboxa.eu/privacy, not just
    /it/privacy or /en/privacy), redirecting to the visitor's language like the homepage already does."""
    for page in sorted(PUBLIC_PAGES):
        write(DIST / page / "index.html", locale_redirect_page(f"{page}/", "Continue"))


def render_redirects() -> None:
    write(DIST / "_redirects", f"""/login {APP_URL}/login 302
/register {APP_URL}/register 302
/verify {APP_URL}/verify 302
/reset-password {APP_URL}/reset-password 302
/app {APP_URL}/app 302
/admin {APP_URL}/admin 302
/api/* {APP_URL}/api/:splat 302
""")


def render_headers() -> None:
    write(DIST / "_headers", """/*
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Content-Security-Policy: default-src 'self'; img-src 'self' data: https://www.google-analytics.com; style-src 'self'; script-src 'self' 'unsafe-inline' https://www.googletagmanager.com; connect-src 'self' https://www.google-analytics.com; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'

/static/*
  Cache-Control: public, max-age=31536000, immutable
""")


def render_robots_and_sitemap() -> None:
    urls = []
    for page in ["", *sorted(PUBLIC_PAGES)]:
        for locale in ("it", "en"):
            path = f"/{locale}/" + page
            base = escape(PUBLIC_URL, quote=True)
            urls.append(
                f"<url><loc>{base}{path}</loc>"
                f"<xhtml:link rel='alternate' hreflang='it' href='{base}/it/{page}'/>"
                f"<xhtml:link rel='alternate' hreflang='en' href='{base}/en/{page}'/>"
                f"<xhtml:link rel='alternate' hreflang='x-default' href='{base}/en/{page}'/>"
                f"<changefreq>{'weekly' if not page else 'monthly'}</changefreq></url>"
            )
    write(DIST / "robots.txt", f"User-agent: *\nAllow: /it/\nAllow: /en/\nSitemap: {PUBLIC_URL}/sitemap.xml\n")
    write(DIST / "sitemap.xml", "<?xml version='1.0' encoding='UTF-8'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9' xmlns:xhtml='http://www.w3.org/1999/xhtml'>" + "".join(urls) + "</urlset>")


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    render_pages()
    render_index()
    render_bare_pages()
    render_redirects()
    render_headers()
    render_robots_and_sitemap()
    copy_assets()
    print(f"Built {DIST}")


if __name__ == "__main__":
    main()
