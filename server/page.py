"""The one page this server serves to a human.

Not a website and not a ladder to browse — the ranking lives in the client. This
exists because the login has to land somewhere: Discord sends the browser back
here, and the last step of connecting a client is a short code that only a
logged-in session can be given.

Before this page existed, a successful login redirected to `/`, which had no
route, so people were told "Not Found" immediately after being signed in
correctly. There is no worse moment to show a 404.

Deliberately one file of inline HTML with no assets, no framework and no
JavaScript beyond a copy button: it is three pieces of text and two forms.
"""

from __future__ import annotations

import html

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 48px 20px; background: #0F1115; color: #F2F4F8;
       font: 15px/1.55 "Segoe UI", system-ui, sans-serif; }
main { max-width: 620px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: #A7B0C0; font-size: 13px; margin: 0 0 28px; }
.card { background: #1E222B; border: 1px solid #2A2F3A; border-radius: 10px;
        padding: 20px 22px; margin: 0 0 14px; }
.label { color: #6B7488; font-size: 11px; font-weight: 600;
         letter-spacing: .04em; text-transform: uppercase; }
.row { display: flex; justify-content: space-between; gap: 16px;
       align-items: baseline; padding: 7px 0; }
.row + .row { border-top: 1px solid #2A2F3A; }
.val { font-weight: 600; }
.muted { color: #6B7488; font-weight: 400; }
.ok { color: #3DD68C; }
.warn { color: #FFB020; }
code { font-family: Consolas, ui-monospace, monospace; }
.code { font-family: Consolas, ui-monospace, monospace; font-size: 30px;
        font-weight: 700; letter-spacing: .08em; color: #FF6B2C;
        margin: 10px 0 6px; }
button, .btn { display: inline-block; border: 0; border-radius: 6px;
        padding: 10px 18px; font: inherit; font-weight: 600; cursor: pointer;
        background: #FF6B2C; color: #160B04; text-decoration: none; }
.btn.sec { background: #262B36; color: #F2F4F8; font-weight: 400; }
form { display: inline; }
ol { padding-left: 20px; color: #A7B0C0; font-size: 14px; }
li { margin: 6px 0; }
"""


def _shell(body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Forts Ladder</title><style>" + _CSS + "</style></head>"
        "<body><main>" + body + "</main></body></html>"
    )


def signed_out(login_url: str) -> str:
    return _shell(
        "<h1>Forts Ladder</h1>"
        "<p class=sub>Sign in to connect the desktop client.</p>"
        "<div class=card>"
        "<p class=label>Step 1</p>"
        f"<p>Sign in with Discord. Only <code>identify</code> is requested — "
        "the account name and id, nothing else.</p>"
        f"<p><a class=btn href='{html.escape(login_url)}'>Sign in with Discord</a></p>"
        "</div>"
        "<div class=card>"
        "<p class=label>Not needed for recording</p>"
        "<p class=sub style='margin:0'>The recorder, the ranking and the report "
        "line all work without an account. Signing in is only needed to queue, "
        "to draft against someone, or to appear on the ladder.</p>"
        "</div>")


def signed_in(*, discord: str | None, ufer_name: str | None,
              steam_id: str | None, consent: bool, role: str,
              steam_url: str, code: str | None) -> str:
    def row(label: str, value: str, cls: str = "") -> str:
        return (f"<div class=row><span class=muted>{html.escape(label)}</span>"
                f"<span class='val {cls}'>{value}</span></div>")

    account = (
        "<div class=card>"
        "<p class=label>Your account</p>"
        + row("Discord", html.escape(discord or "—"))
        + row("Ladder name", html.escape(ufer_name or "not set"),
              "" if ufer_name else "warn")
        + row("Steam", f"<code>{html.escape(steam_id)}</code>" if steam_id
              else "<span class=warn>not linked</span>")
        + row("Role", html.escape(role))
        + row("Tracked", "<span class=ok>yes</span>" if consent
              else "<span class=warn>no — results are not rated</span>")
        + "</div>")

    # Steam is what ties a Discord account to matches in the game log, so an
    # account without it is inert. Say that where it is missing.
    steam_block = "" if steam_id else (
        "<div class=card>"
        "<p class=label>Link Steam</p>"
        "<p>Your matches are identified by Steam ID, so without this the ladder "
        "cannot tell which games are yours. Steam is asked directly; no API key "
        "and no password are involved.</p>"
        f"<p><a class=btn href='{html.escape(steam_url)}'>Link Steam account</a></p>"
        "</div>")

    consent_block = (
        "<div class=card>"
        "<p class=label>Being tracked</p>"
        + ("<p>Your results count towards the ladder. You can withdraw at any "
           "time; past results stop counting as well, because the rating is "
           "recomputed from scratch.</p>"
           "<form method=post action='/me/consent/off'>"
           "<button class='btn sec'>Stop tracking my results</button></form>"
           if consent else
           "<p>Nothing of yours is rated yet. Matches only count once you agree "
           "to it — and only matches this ladder set up, with everyone in them "
           "agreeing too.</p>"
           "<form method=post action='/me/consent/on'>"
           "<button>Track my results</button></form>")
        + "</div>")

    pair_block = (
        "<div class=card>"
        "<p class=label>Connect the client</p>"
        + (f"<div class=code id=code>{html.escape(code)}</div>"
           "<p class=sub style='margin:0 0 12px'>Valid for five minutes, and "
           "usable once. Enter it in the client under Live.</p>"
           "<button class='btn sec' onclick=\"navigator.clipboard"
           ".writeText(document.getElementById('code').textContent.trim())\">"
           "Copy</button> "
           if code else
           "<p>The desktop client cannot see this browser's session, so it needs "
           "a short code once.</p>")
        + "<form method=post action='/auth/pair/page'>"
          "<button" + (" class='btn sec'" if code else "") + ">"
        + ("New code" if code else "Show code") + "</button></form>"
        "</div>")

    return _shell(
        "<h1>Signed in</h1>"
        "<p class=sub>This page exists to connect the client. The ranking "
        "itself lives in the client, not here.</p>"
        + account + steam_block + consent_block + pair_block
        + "<div class=card><p class=label>Sign out</p>"
          "<form method=post action='/auth/logout/page'>"
          "<button class='btn sec'>Sign out of this browser</button></form></div>")
