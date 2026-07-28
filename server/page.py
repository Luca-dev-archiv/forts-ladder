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
select, textarea, input { font: inherit; color: #F2F4F8; background: #262B36;
        border: 1px solid #2A2F3A; border-radius: 6px; padding: 9px; }
select { min-width: 180px; }
a { color: #FF9E6B; }
p { margin: 10px 0; }
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
              steam_url: str, code: str | None,
              is_admin: bool = False, can_host: bool = False) -> str:
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
        + _nav(is_admin, can_host)
        + account + steam_block + consent_block + pair_block
        + "<div class=card><p class=label>Sign out</p>"
          "<form method=post action='/auth/logout/page'>"
          "<button class='btn sec'>Sign out of this browser</button></form></div>")


# --------------------------------------------------------------------- Nav

def _nav(is_admin: bool, can_host: bool) -> str:
    """Links to whatever this account may reach. Nothing it may not."""
    return ("<p class=sub style='margin:-18px 0 24px'><a href='/'>Account</a>"
            + (" &nbsp;·&nbsp; <a href='/manage/tournaments'>Tournaments</a>"
               if can_host else "")
            + (" &nbsp;·&nbsp; <a href='/admin'>Admin</a>" if is_admin else "")
            + "</p>")


ROLE_NAMES = ["guest", "player", "caster", "admin", "owner"]


def _error(message: str) -> str:
    """A refusal belongs next to the form that caused it, not on a JSON page."""
    return (f"<div class=card><p class=warn style='margin:0'>"
            f"{html.escape(message)}</p></div>" if message else "")


def _input(name: str, placeholder: str = "", value: str = "",
           width: str = "100%") -> str:
    return (f"<input name='{html.escape(name)}' value='{html.escape(value)}' "
            f"placeholder='{html.escape(placeholder)}' "
            f"style='width:{width};padding:9px;border-radius:6px;"
            "border:1px solid #2A2F3A;background:#262B36;color:#F2F4F8;"
            "font:inherit'>")


# ------------------------------------------------------------------- Admin

def admin(*, accounts: list[dict], grants: list[str], my_id: str,
          pools: dict, ranking_count: int, may_set_roles: bool = True,
          error: str = "") -> str:
    """Accounts, roles and grants.

    A list, because the whole job is "who may do what". Each row submits on its
    own, so a mistake affects one person instead of the whole table.
    """
    rows = []
    for a in accounts:
        checks = "".join(
            "<label style='margin-right:12px;white-space:nowrap'>"
            f"<input type=checkbox name=grant value='{html.escape(g)}'"
            + (" checked" if g in a["grants"] else "") + "> "
            + html.escape(g.replace("_", " ")) + "</label>"
            for g in grants)
        options = "".join(
            f"<option value='{r}'"
            + (" selected" if r == a["role"].lower() else "") + f">{r}</option>"
            for r in ROLE_NAMES)
        # Your own row carries no controls: demoting yourself is the one mistake
        # here that cannot be undone from this page. And only an owner sees the
        # role picker at all, rather than one that refuses on every submit.
        controls = ("<span class=muted>this is you</span>" if a["id"] == my_id else
                    "<form method=post action='/admin/save'>"
                    f"<input type=hidden name=account value='{html.escape(a['id'])}'>"
                    + (f"<p><select name=role>{options}</select></p>"
                       if may_set_roles else "")
                    + f"<p>{checks}</p>"
                    "<button class='btn sec'>Save</button></form>")
        rows.append(
            "<div class=card>"
            f"<div class=row><span class=val>{html.escape(a['discord'] or '?')}</span>"
            f"<span class=muted>{html.escape(a['role'])}</span></div>"
            f"<div class=row><span class=muted>ladder name</span>"
            f"<span>{html.escape(a['ufer_name'] or '—')}</span></div>"
            "<div class=row><span class=muted>Steam</span>"
            + (f"<code>{html.escape(a['steam_id'])}</code>" if a["steam_id"]
               else "<span class=warn>not linked</span>")
            + "</div><div class=row><span class=muted>tracked</span>"
            + ("<span class=ok>yes</span>" if a["tracked"]
               else "<span class=warn>no</span>")
            + f"</div><div style='margin-top:12px'>{controls}</div></div>")

    pool_row = ("<div class=row><span class=muted>map pool</span>"
                f"<span class=val>{pools['maps']} maps, "
                f"{pools['commanders']} commanders</span></div>"
                if pools["configured"] else
                "<div class=row><span class=muted>map pool</span>"
                "<span class='val warn'>not set</span></div>"
                "<p class=sub>The server cannot read the game files, so an "
                "admin publishes the pool once from a client that has Forts "
                "installed — in the client, under Play.</p>")

    setup = ("<div class=card><p class=label>Server setup</p>" + pool_row
             + "<div class=row><span class=muted>ranking</span>"
               f"<span class=val>{ranking_count} players</span></div>"
               "<form method=post action='/admin/ranking/reload/page' "
               "style='margin-top:12px'>"
               "<button class='btn sec'>Re-read the ranking file</button>"
               "</form></div>")

    return _shell(
        "<h1>Admin</h1>"
        "<p class=sub>Who may do what, and whether this server is set up.</p>"
        + _nav(True, True) + _error(error) + setup
        + f"<p class=label>{len(accounts)} account(s)</p>"
        + ("".join(rows) or
           "<div class=card><p>Nobody has signed in yet.</p></div>"))


# -------------------------------------------------------------- Tournaments

def tournaments(*, listing: list[dict], is_admin: bool,
                modes: list[tuple[str, str]], error: str = "",
                name: str = "", entrants: str = "") -> str:
    options = "".join(f"<option value='{html.escape(k)}'>{html.escape(v)}</option>"
                      for k, v in modes)
    rows = "".join(
        "<div class=card>"
        f"<div class=row><a class=val href='/manage/tournaments/{html.escape(t['id'])}'>"
        f"{html.escape(t['name'])}</a>"
        f"<span class=muted>{t['participants']} entrants</span></div>"
        f"<div class=row><span class=muted>{html.escape(t['mode_key'])}</span>"
        + ("<span class=ok>finished</span>" if t["finished"]
           else "<span class=warn>running</span>")
        + "</div></div>"
        for t in listing)

    return _shell(
        "<h1>Tournaments</h1>"
        "<p class=sub>Seeding comes from the ratings you enter, byes go to the "
        "top seeds, and results are reported rather than inferred.</p>"
        + _nav(is_admin, True) + _error(error)
        + "<div class=card><p class=label>New tournament</p>"
          "<form method=post action='/manage/tournaments'>"
          "<p>" + _input("name", "Name", name) + "</p>"
          f"<p><select name=mode>{options}</select></p>"
          "<p class=label>Entrants — one per line, "
          "optionally <code>name, rating</code></p>"
          "<p><textarea name=entrants rows=8 style='width:100%;padding:9px;"
          "border-radius:6px;border:1px solid #2A2F3A;background:#262B36;"
          "color:#F2F4F8;font:inherit;font-family:Consolas,monospace'>"
          + html.escape(entrants) + "</textarea></p>"
          "<p class=sub style='margin:0 0 12px'>Ratings decide the seeding; "
          "leave one off and it counts as 1000. A bracket cannot be changed "
          "once it exists, because entrants are already seeded against each "
          "other.</p>"
          "<button>Create</button></form></div>"
        + (f"<p class=label>{len(listing)} tournament(s)</p>" + rows if listing
           else "<div class=card><p>None yet.</p></div>"))


def bracket(*, name: str, mode: str, rounds: list[dict], champion: str | None,
            tid: str, is_admin: bool, best_of: int, error: str = "",
            can_report: bool = True, can_host: bool = True) -> str:
    """The bracket, with a result form on each match that can be played.

    Readable by anyone with an account — an entrant wants to see the bracket
    they are in. Only a referee or host gets the report form.
    """
    blocks = []
    for r in rounds:
        cards = []
        for m in r["matches"]:
            a, b = m.get("a_name"), m.get("b_name")
            if m["winner"]:
                state = (f"<span class=ok>{html.escape(m['winner'])}</span>"
                         + (f" &nbsp;{m['score'][0]}:{m['score'][1]}"
                            if m.get("score") else "")
                         + (" <span class=muted>(bye)</span>" if m["bye"] else ""))
                form = ""
            elif m["ready"] and not can_report:
                state = "<span class=warn>waiting for a result</span>"
                form = ""
            elif m["ready"]:
                state = "<span class=warn>waiting for a result</span>"
                # The winner is picked from the two entrants rather than typed:
                # a free text field can name someone who is not in the match.
                form = (
                    "<form method=post action='/manage/tournaments/"
                    f"{html.escape(tid)}/report'>"
                    f"<input type=hidden name=match value='{html.escape(m['id'])}'>"
                    "<p><select name=winner>"
                    f"<option value='{html.escape(a or '')}'>{html.escape(a or '?')}</option>"
                    f"<option value='{html.escape(b or '')}'>{html.escape(b or '?')}</option>"
                    "</select> "
                    + _input("score", f"{best_of // 2 + 1}:0", width="70px")
                    + "</p><button class='btn sec'>Report</button></form>")
            else:
                state = "<span class=muted>waiting for entrants</span>"
                form = ""
            cards.append(
                "<div class=card>"
                "<div class=row><span class=val>"
                f"{html.escape(a or '—')} vs {html.escape(b or '—')}</span>"
                f"<span class=muted>{html.escape(m['id'])}</span></div>"
                f"<div class=row><span class=muted>result</span>"
                f"<span>{state}</span></div>"
                + (f"<div style='margin-top:12px'>{form}</div>" if form else "")
                + "</div>")
        blocks.append(f"<p class=label>{html.escape(r['round'])}</p>"
                      + "".join(cards))

    head = (f"<h1>{html.escape(name)}</h1>"
            f"<p class=sub>{html.escape(mode)} · Bo{best_of}"
            + (f" · <span class=ok>won by {html.escape(champion)}</span>"
               if champion else "")
            + "</p>")
    return _shell(head + _nav(is_admin, can_host) + _error(error)
                  + ("<p><a class='btn sec' href='/manage/tournaments'>"
                     "All tournaments</a></p>" if can_host else "")
                  + "".join(blocks))
