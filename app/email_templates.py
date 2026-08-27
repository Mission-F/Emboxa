from __future__ import annotations

from html import escape


PALETTE = {"navy": "#14213d", "orange": "#fca311", "grey": "#e5e5e5", "white": "#ffffff"}


def transactional_email(
    *,
    title: str,
    intro: str,
    support_email: str,
    public_url: str,
    code: str | None = None,
    code_caption: str | None = None,
    action_label: str | None = None,
    action_url: str | None = None,
    note: str = "If you did not request this, you can safely ignore this email.",
    logo_url: str | None = None,
    footer_text: str = "MissionF",
) -> tuple[str, str]:
    """Return a conservative multipart email body shared by all transactional messages.

    The code is never split with a space: it must paste into the 6-digit verification
    field exactly as shown. Visual grouping comes only from CSS letter-spacing.
    """
    plain = ["EMBOXA", "", title, "", intro]
    if code:
        plain.extend(["", code, ""])
    if action_label and action_url:
        plain.extend(["", f"{action_label}: {action_url}"])
    plain.extend(["", note, "", footer_text, support_email])

    safe_title = escape(title)
    safe_intro = escape(intro)
    safe_note = escape(note)
    safe_support = escape(support_email)
    safe_footer = escape(footer_text)
    logo = logo_url or f"{public_url.rstrip('/')}/static/icons/emboxa-192.png"
    code_html = ""
    if code:
        caption_html = (
            f'<div style="margin-top:10px;color:#8b93a3;font:12px/1.5 Arial,sans-serif">{escape(code_caption)}</div>'
            if code_caption else ""
        )
        code_html = (
            '<tr><td style="padding:8px 32px 30px">'
            f'<div style="padding:20px 16px;border:1px solid {PALETTE["orange"]};border-radius:12px;'
            f'background:#fff8ec;color:{PALETTE["navy"]};font:700 32px/1.2 \'Courier New\',Courier,monospace;'
            f'letter-spacing:10px;text-align:center;-webkit-user-select:all;user-select:all">{escape(code)}</div>'
            f'{caption_html}</td></tr>'
        )
    cta_html = ""
    if action_label and action_url:
        cta_html = (
            '<tr><td align="center" style="padding:4px 32px 28px">'
            f'<a href="{escape(action_url, quote=True)}" style="display:inline-block;padding:13px 22px;'
            f'border-radius:10px;background:{PALETTE["orange"]};color:#000000;text-decoration:none;'
            f'font:700 15px Arial,sans-serif">{escape(action_label)}</a></td></tr>'
        )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head><body style="margin:0;padding:0;background:#f3f3f1;color:{PALETTE['navy']}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f3f3f1"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="max-width:560px;border:1px solid {PALETTE['grey']};border-radius:18px;background:{PALETTE['white']}">
<tr><td style="padding:26px 32px 18px"><table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr>
<td><img src="{escape(logo, quote=True)}" width="44" height="44" alt="EMBOXA" style="display:block;border:0;border-radius:11px"></td>
<td style="padding-left:12px;color:{PALETTE['navy']};font:800 18px Arial,sans-serif;letter-spacing:3px">EMBOXA</td>
</tr></table></td></tr>
<tr><td style="padding:12px 32px 8px"><h1 style="margin:0;color:{PALETTE['navy']};font:800 30px/1.18 Arial,sans-serif">{safe_title}</h1></td></tr>
<tr><td style="padding:8px 32px 22px;color:#596579;font:16px/1.65 Arial,sans-serif">{safe_intro}</td></tr>
{code_html}{cta_html}
<tr><td style="padding:4px 32px 28px;color:#7a8493;font:13px/1.55 Arial,sans-serif">{safe_note}</td></tr>
<tr><td style="padding:22px 32px;border-top:1px solid {PALETTE['grey']};color:#7a8493;font:12px/1.6 Arial,sans-serif">
<strong style="color:{PALETTE['navy']}">{safe_footer}</strong><br><a href="mailto:{safe_support}" style="color:{PALETTE['navy']}">{safe_support}</a>
</td></tr></table></td></tr></table></body></html>"""
    return "\n".join(plain), html


VERIFY_COPY = {
    "it": ("Verifica la tua email", "Usa questo codice per completare la registrazione gratuita a EMBOXA. Scade tra 15 minuti.", "Incollalo così com'è nella pagina di verifica."),
    "en": ("Verify your email", "Use this code to complete your free EMBOXA account registration. It expires in 15 minutes.", "Paste it exactly as shown on the verification page."),
    "fr": ("Vérifiez votre e-mail", "Utilisez ce code pour terminer votre inscription gratuite à EMBOXA. Il expire dans 15 minutes.", "Collez-le tel quel sur la page de vérification."),
    "de": ("E-Mail bestätigen", "Verwenden Sie diesen Code, um Ihre kostenlose EMBOXA-Registrierung abzuschließen. Er läuft in 15 Minuten ab.", "Fügen Sie ihn genau so auf der Bestätigungsseite ein."),
    "es": ("Verifica tu correo", "Usa este código para completar tu registro gratuito en EMBOXA. Caduca en 15 minutos.", "Pégalo tal cual en la página de verificación."),
    "pt": ("Verifique o seu e-mail", "Use este código para concluir o registo gratuito na EMBOXA. Expira em 15 minutos.", "Cole-o exatamente assim na página de verificação."),
}

RESET_COPY = {
    "it": ("Reimposta la password", "Abbiamo ricevuto una richiesta per scegliere una nuova password EMBOXA. Il link scade tra un'ora.", "Reimposta password"),
    "en": ("Reset your password", "We received a request to choose a new password for your EMBOXA account. This link expires in one hour.", "Reset password"),
    "fr": ("Réinitialisez votre mot de passe", "Nous avons reçu une demande de nouveau mot de passe EMBOXA. Ce lien expire dans une heure.", "Réinitialiser"),
    "de": ("Passwort zurücksetzen", "Wir haben eine Anfrage für ein neues EMBOXA-Passwort erhalten. Dieser Link läuft in einer Stunde ab.", "Passwort zurücksetzen"),
    "es": ("Restablece tu contraseña", "Hemos recibido una solicitud para elegir una nueva contraseña de EMBOXA. Este enlace caduca en una hora.", "Restablecer contraseña"),
    "pt": ("Redefina a palavra-passe", "Recebemos um pedido para escolher uma nova palavra-passe EMBOXA. Esta ligação expira numa hora.", "Redefinir palavra-passe"),
}

NOTE_COPY = {
    "it": "Se non hai richiesto tu questa email, puoi ignorarla in tutta sicurezza.",
    "en": "If you did not request this, you can safely ignore this email.",
    "fr": "Si vous n’êtes pas à l’origine de cette demande, vous pouvez ignorer cet e-mail en toute sécurité.",
    "de": "Wenn Sie dies nicht angefordert haben, können Sie diese E-Mail ignorieren.",
    "es": "Si no has solicitado esto, puedes ignorar este correo sin problema.",
    "pt": "Se não foi você a pedir isto, pode ignorar este e-mail com segurança.",
}


def verification_email(code: str, *, support_email: str, public_url: str, logo_url: str = "", footer_text: str = "MissionF", locale: str = "en") -> tuple[str, str]:
    title, intro, caption = VERIFY_COPY.get(locale, VERIFY_COPY["en"])
    return transactional_email(
        title=title,
        intro=intro,
        code=code,
        code_caption=caption,
        note=NOTE_COPY.get(locale, NOTE_COPY["en"]),
        support_email=support_email,
        public_url=public_url,
        logo_url=logo_url or None,
        footer_text=footer_text,
    )


def password_reset_email(url: str, *, support_email: str, public_url: str, logo_url: str = "", footer_text: str = "MissionF", locale: str = "en") -> tuple[str, str]:
    title, intro, action = RESET_COPY.get(locale, RESET_COPY["en"])
    return transactional_email(
        title=title,
        intro=intro,
        action_label=action,
        action_url=url,
        note=NOTE_COPY.get(locale, NOTE_COPY["en"]),
        support_email=support_email,
        public_url=public_url,
        logo_url=logo_url or None,
        footer_text=footer_text,
    )


def test_email(*, support_email: str, public_url: str, logo_url: str = "", footer_text: str = "MissionF") -> tuple[str, str]:
    return transactional_email(
        title="Email delivery is ready",
        intro="SMTP delivery from EMBOXA is working correctly. Transactional messages will use this branded template.",
        support_email=support_email,
        public_url=public_url,
        logo_url=logo_url or None,
        footer_text=footer_text,
    )
