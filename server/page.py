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
import json

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


def _shell(body: str, head: str = "") -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Forts Ladder</title><style>" + _CSS + "</style>"
        + head +
        "</head><body><main>" + body + "</main></body></html>"
    )


#: The bracket viewer's stylesheet plus the variables that make it match the
#: rest of this page. Only loaded where a bracket is drawn — every other page
#: is still one file with no assets.
_VIEWER_HEAD = (
    "<link rel=stylesheet href='/static/brackets-viewer.min.css'>"
    "<style>"
    ":root{"
    "--primary-background:#1E222B;"
    "--secondary-background:#262B36;"
    "--match-background:#1E222B;"
    "--font-color:#F2F4F8;"
    "--win-color:#3DD68C;"
    "--loss-color:#F0616D;"
    "--label-color:#6B7488;"
    "--hint-color:#6B7488;"
    "--connector-color:#2A2F3A;"
    "--border-color:#2A2F3A;"
    "--border-hover-color:#FF6B2C;"
    "--round-margin:22px;"
    "--match-width:200px;"
    "--match-horizontal-padding:10px;"
    "--match-vertical-padding:8px;"
    "}"
    # 620px is right for a form and far too narrow for four rounds of 240px, so
    # this one page gets the room. The viewer scrolls inside itself beyond that
    # rather than making the whole page scroll sideways.
    "main{max-width:1240px;}"
    ".brackets-viewer{overflow-x:auto;}"
    # Its own h1 duplicates ours, and its group title says "Round 1" for a
    # single-elimination stage that has no groups worth naming.
    ".brackets-viewer h1,.brackets-viewer .group-name{display:none;}"
    "</style>")


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
              is_admin: bool = False, can_host: bool = False,
              pending_name: str | None = None) -> str:
    def row(label: str, value: str, cls: str = "") -> str:
        return (f"<div class=row><span class=muted>{html.escape(label)}</span>"
                f"<span class='val {cls}'>{value}</span></div>")

    account = (
        "<div class=card>"
        "<p class=label>Your account</p>"
        + row("Discord", html.escape(discord or "—"))
        + row("Ladder name",
              html.escape(ufer_name) if ufer_name
              else f"<span class=warn>{html.escape(pending_name)} — waiting "
                   "for an admin</span>" if pending_name
              else "<span class=warn>not set</span>")
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


def _for_script(value) -> str:
    """JSON that cannot climb out of the <script> element it sits in.

    `json.dumps` escapes quotes but not angle brackets, so a tournament called
    `</script><img src=x onerror=…>` ends the script block and runs. The three
    escapes below are valid JSON — the parser sees the same string — and inert
    in HTML. The existing markup-injection test is what caught this.
    """
    # The replacements are the six-character escape *sequences*, not the
    # characters they stand for: "\u003c" written in Python source is just "<"
    # again, a no-op that looks exactly like a fix. Hence the raw strings.
    return (json.dumps(value)
            .replace("<", r"\u003c")
            .replace(">", r"\u003e")
            .replace("&", r"\u0026"))


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
          error: str = "", claims: list[dict] | None = None) -> str:
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
            # The display name, with the id only as a tooltip: a 17-digit
            # number tells a human nothing, and this list is read by a human.
            + (f"<span title='{html.escape(a['steam_id'])}'>"
               + html.escape(a.get("steam_name") or a["steam_id"]) + "</span>"
               if a["steam_id"] else "<span class=warn>not linked</span>")
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

    # Held ladder-name claims, first: somebody is waiting on each of these.
    pending = ""
    for c in (claims or []):
        pending += (
            "<div class=card>"
            f"<div class=row><span class=val>{html.escape(c['claim'])}</span>"
            "<span class=muted>claimed by "
            f"{html.escape(c['discord'] or '?')}</span></div>"
            "<div class=row><span class=muted>Steam</span><span>"
            + html.escape(c.get("steam_name") or c.get("steam_id") or "—")
            + "</span></div>"
            "<p class=sub>The spreadsheet lists Discord names and this one "
            "differs — which is common, and why it takes a person.</p>"
            "<form method=post action='/admin/name'>"
            f"<input type=hidden name=account value='{html.escape(c['id'])}'>"
            "<button name=decision value=confirm>Confirm</button> "
            "<button class='btn sec' name=decision value=reject>Reject</button>"
            "</form></div>")
    if pending:
        pending = ("<p class=label>Ladder names waiting for confirmation</p>"
                   + pending)

    return _shell(
        "<h1>Admin</h1>"
        "<p class=sub>Who may do what, and whether this server is set up.</p>"
        + _nav(True, True) + _error(error) + pending + setup
        + f"<p class=label>{len(accounts)} account(s)</p>"
        + ("".join(rows) or
           "<div class=card><p>Nobody has signed in yet.</p></div>"))


# -------------------------------------------------------------- Tournaments

def tournaments(*, listing: list[dict], is_admin: bool,
                modes: list[tuple[str, str]], error: str = "",
                name: str = "", entrants: str = "",
                can_create: bool = True) -> str:
    """The list, and — for whoever may create one — the form.

    A referee sees the list without the form: they report into brackets they
    did not build, and would otherwise have no way to find one.
    """
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
        + ("<div class=card><p class=label>New tournament</p>"
          "<form method=post action='/manage/tournaments'>"
          "<p>" + _input("name", "Name", name) + "</p>"
          f"<p><select name=mode>{options}</select>"
          " <select name=best_of>"
          "<option value=''>series length from the mode</option>"
          "<option value='1'>Bo1</option>"
          "<option value='3'>Bo3</option>"
          "<option value='5'>Bo5</option>"
          "<option value='7'>Bo7</option>"
          "</select>"
          " <select name=seeding>"
          "<option value='rating'>seed by rating</option>"
          "<option value='listed'>seed in the order listed</option>"
          "<option value='random'>seed at random</option>"
          "</select></p>"
          "<p class=label>Entrants — one per line, "
          "optionally <code>name, rating</code></p>"
          "<p><textarea name=entrants rows=8 style='width:100%;padding:9px;"
          "border-radius:6px;border:1px solid #2A2F3A;background:#262B36;"
          "color:#F2F4F8;font:inherit;font-family:Consolas,monospace'>"
          + html.escape(entrants) + "</textarea></p>"
          "<p class=sub style='margin:0 0 12px'>Byes go to the top seeds, and "
          "seeds 1 and 2 can only meet in the final. Seeding by rating uses "
          "the numbers you type here (a missing one counts as 1000); seeding "
          "in the listed order ignores them, which is what you want when you "
          "already know the pairings you intend. Entrants can be renamed until "
          "the first result is reported.</p>"
          "<button>Create</button></form></div>" if can_create else "")
        + (f"<p class=label>{len(listing)} tournament(s)</p>" + rows if listing
           else "<div class=card><p>None yet.</p></div>"))


def bracket(*, name: str, mode: str, rounds: list[dict], champion: str | None,
            tid: str, is_admin: bool, best_of: int, error: str = "",
            can_report: bool = True, can_host: bool = True,
            entrants: list[dict] | None = None, editable: bool = False,
            data: dict | None = None) -> str:
    """The bracket, drawn as one, with a result form per open match.

    The drawing is `brackets-viewer.js` — connector lines, byes that skip a
    round, boxes spread so the pairings line up. Written by hand this was a
    column of cards per round, which is not a bracket: it made it impossible to
    see who could meet whom, which is the only thing a bracket is for. The
    library is vendored rather than loaded from a CDN, because a ladder that is
    reachable through a tunnel should not stop drawing when jsdelivr is
    unreachable.

    The forms below it are plain HTML on purpose. The viewer can call back on a
    click, but a result is a thing you type a score into, and a form that works
    without any JavaScript is one less thing that can fail during a tournament.

    Readable by anyone signed in — an entrant wants to see their own bracket.
    Only a host or referee gets the forms.
    """
    head = (f"<h1>{html.escape(name)}</h1>"
            f"<p class=sub>{html.escape(mode)} · Bo{best_of}"
            + (f" · <span class=ok>won by {html.escape(champion)}</span>"
               if champion else "")
            + "</p>")

    # The report forms, one per match that can be played. Grouped by round so
    # the list reads in the same order as the bracket above it.
    forms = ""
    if can_report:
        blocks = []
        for r in rounds:
            open_ones = [m for m in r["matches"]
                         if m["ready"] and not m["winner"]]
            for m in open_ones:
                a, b = m.get("a_name"), m.get("b_name")
                blocks.append(
                    "<div class=card>"
                    f"<div class=row><span class=val>{html.escape(a or '—')}"
                    f" vs {html.escape(b or '—')}</span>"
                    f"<span class=muted>{html.escape(r['round'])}</span></div>"
                    "<form method=post action='/manage/tournaments/"
                    f"{html.escape(tid)}/report' style='margin-top:10px'>"
                    f"<input type=hidden name=match value='{html.escape(m['id'])}'>"
                    # Picked from the two entrants rather than typed: free text
                    # can name somebody who is not in the match.
                    "<select name=winner>"
                    f"<option value='{html.escape(a or '')}'>{html.escape(a or '?')}</option>"
                    f"<option value='{html.escape(b or '')}'>{html.escape(b or '?')}</option>"
                    "</select> "
                    + _input("score", f"{best_of // 2 + 1}:0", width="80px")
                    + " <button class='btn sec'>Report</button></form></div>")
        forms = ("<p class=label>Waiting for a result</p>" + "".join(blocks)
                 if blocks else
                 "<div class=card><p class=sub style='margin:0'>Nothing to "
                 "report right now.</p></div>")

    # Renaming, while it is still harmless: a typo is the commonest thing to fix
    # and used to mean building the bracket again from scratch.
    rename = ""
    if editable and entrants:
        rows = "".join(
            "<div class=row>"
            f"<span class=muted>seed {e['seed']}</span>"
            "<span><form method=post action='/manage/tournaments/"
            f"{html.escape(tid)}/rename' style='display:inline'>"
            f"<input type=hidden name=seat value='{e['seat']}'>"
            + _input("name", "", e["name"], width="220px")
            + " <button class='btn sec'>Rename</button></form></span></div>"
            for e in entrants)
        rename = ("<div class=card><p class=label>Entrants</p>" + rows
                  + "<p class=sub>Possible until the first result is reported: "
                    "after that the pairings rest on these names.</p></div>")

    payload = _for_script(data or {})
    viewer = (
        "<div class=card style='padding:10px'>"
        "<div class='brackets-viewer'></div></div>"
        "<script src='/static/brackets-viewer.min.js'></script>"
        "<script>\n"
        "// Round names come from us, not from the library's counter: "
        "\"Semi-final\" beats \"Round 2\", and the server already computes "
        "them for the client too.\n"
        f"const data = {payload};\n"
        "const names = " + _for_script(
            {r["round_key"] if "round_key" in r else str(i + 1): r["round"]
             for i, r in enumerate(rounds)}) + ";\n"
        "window.bracketsViewer.render(data, {\n"
        "  customRoundName: (info) => Object.values(names)[info.roundNumber - 1],\n"
        "  clear: true,\n"
        "}).catch(e => {\n"
        "  // Say so rather than leaving an empty box: a bracket that failed to\n"
        "  // draw looks exactly like a tournament with no matches.\n"
        "  document.querySelector('.brackets-viewer').textContent =\n"
        "    'The bracket could not be drawn: ' + e.message;\n"
        "});\n"
        "</script>")

    return _shell(head + _nav(is_admin, can_host) + _error(error)
                  + ("<p><a class='btn sec' href='/manage/tournaments'>"
                     "All tournaments</a></p>" if can_host else "")
                  + viewer + forms + rename, head=_VIEWER_HEAD)
