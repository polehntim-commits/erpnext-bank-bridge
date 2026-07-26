# SPDX-License-Identifier: MIT
"""v0.7.2 — generate the Claude Desktop / Claude Code MCP client config.

Connecting an AI client to Bank Bridge's MCP endpoint (v0.6.0) meant hand-editing
`claude_desktop_config.json`: find the file, get the JSON shape right, paste a
bearer token without mangling it, restart. Four steps, each with its own way to
fail silently. This module produces the exact config so /admin/mcp can hand it
over as one copy or one download.

WHY THE URL IS ALWAYS THE LAN URL — the load-bearing decision here. It would be
easy to hand over the Tailscale Funnel URL when v0.7.1's sidecar reports one, and
it would be wrong twice over:

  1. IT WOULD NOT WORK. The serve config Bank Bridge writes publishes exactly one
     path, `/bankbridge/plaid/oauth_return` (see tailscale_sidecar.
     build_serve_config). `https://<funnel-host>/mcp` is not published, so it
     404s — and a config that fails only at connect time is worse than no config.
  2. MAKING IT WORK WOULD BE A REGRESSION. /mcp is the AI-operable surface. v0.6.0
     put it deliberately outside the `/bankbridge/` Funnel prefix, and v0.7.0's
     path restriction exists precisely so nothing but the OAuth callback is
     public. Publishing /mcp would put mutating tools on the Internet behind a
     bearer token that, in the shipped compose, equals APP_SEED.

Claude Desktop runs on the operator's own machine, on the same LAN as the Umbrel,
so the LAN URL is not a compromise — it is the correct answer. The widget says so
rather than leaving the operator to wonder why their Funnel isn't used.

THE HEADER IS SPLIT ACROSS `args` AND `env`, which looks like a quirk and is a
documented upstream bug: Claude Desktop on Windows (and Cursor) fail to escape
spaces inside `args` when invoking npx, mangling any value that contains one. So
`--header "Authorization: Bearer <token>"` breaks there. mcp-remote's own README
prescribes the fix used here — a space-free `Authorization:${AUTH_HEADER}` in
args, with the space-bearing `Bearer <token>` in `env`. Same result, works
everywhere.

NO TOKEN IS EVER RENDERED INTO THE PAGE. v0.6.0 established that /admin/mcp shows
only a masked token; a config preview containing the real one would quietly undo
that. The preview carries a masked token and the buttons fetch the real config
from /admin/mcp/claude_desktop_config.json, so the secret reaches the clipboard
or a downloaded file but never the HTML — which is what gets screenshotted,
"save page as"-ed, and pasted into bug reports.
"""
import json

#: The name the server appears under in the client's config, and therefore the
#: prefix on every tool the AI sees (`bankbridge__get_reconciliation_status`).
#: Matches the `bankbridge` path prefix convention.
SERVER_KEY = 'bankbridge'

#: Bank Bridge's MCP endpoint. NOT under `/bankbridge/` — see the module note.
MCP_PATH = '/mcp'

#: Default config locations per OS, as documented by Claude Desktop.
CONFIG_PATHS = {
    'macos': '~/Library/Application Support/Claude/claude_desktop_config.json',
    'windows': r'%APPDATA%\Claude\claude_desktop_config.json',
    'linux': '~/.config/Claude/claude_desktop_config.json',
}

OS_LABELS = {'macos': 'macOS', 'windows': 'Windows', 'linux': 'Linux'}

#: How to fully quit, per OS — a restart is required for the client to re-read
#: its config, and "close the window" is not a restart.
QUIT_HINTS = {
    'macos': '⌘Q (Cmd-Q) — closing the window is not enough',
    'windows': 'Alt+F4, or right-click the tray icon and Quit',
    'linux': 'quit from the app menu, not just the window close button',
}

#: Mobile UA markers. An iPhone's UA contains "like Mac OS X", so these are
#: checked FIRST — otherwise a phone would be told it is a Mac and shown a path
#: it cannot use. Claude Desktop has no mobile build, so these resolve to
#: "unknown" and the widget lists every path instead of guessing.
_MOBILE_MARKERS = ('iphone', 'ipad', 'ipod', 'android', 'mobile')


def detect_os(user_agent: str | None) -> str:
    """'macos' | 'windows' | 'linux' | '' from a User-Agent string.

    Advisory only — the widget always labels the result as a default location
    that may differ, and offers the other paths. A wrong guess must never be
    the reason a config lands in the wrong place."""
    ua = (user_agent or '').lower()
    if not ua:
        return ''
    if any(m in ua for m in _MOBILE_MARKERS):
        return ''
    if 'windows' in ua:
        return 'windows'
    if 'macintosh' in ua or 'mac os' in ua:
        return 'macos'
    if 'linux' in ua or 'x11' in ua:
        return 'linux'
    return ''


def config_path_for(os_name: str) -> str:
    """The default config file path for a detected OS, or '' if unknown."""
    return CONFIG_PATHS.get(os_name, '')


def mcp_url(host: str, scheme: str = 'http') -> str:
    """Bank Bridge's MCP endpoint as the operator's browser reached it.

    Derived from the request rather than configured, so it is right by
    construction whichever way the admin UI was opened — `umbrel.local:5202`, a
    LAN IP, or a hostname behind Umbrel's app_proxy. See the module docstring for
    why a Funnel URL is deliberately never used here."""
    host = (host or '').strip()
    if not host:
        return ''
    scheme = (scheme or 'http').strip().lower()
    if scheme not in ('http', 'https'):
        scheme = 'http'
    return f'{scheme}://{host}{MCP_PATH}'


def config_entry(url: str, token: str) -> dict:
    """The `mcpServers` object to merge into claude_desktop_config.json.

    `--transport http-only` because Bank Bridge implements the Streamable-HTTP
    request/response surface and no SSE endpoint; letting mcp-remote try SSE
    first would just cost a failed probe on every start. `--allow-http` because
    the URL is a plain-http LAN address, which mcp-remote refuses without it."""
    return {
        'mcpServers': {
            SERVER_KEY: {
                'command': 'npx',
                'args': [
                    '-y', 'mcp-remote',
                    url,
                    '--transport', 'http-only',
                    '--allow-http',
                    # Space-free on purpose — see the module docstring. The
                    # space-bearing value lives in `env` below.
                    '--header', 'Authorization:${AUTH_HEADER}',
                ],
                'env': {'AUTH_HEADER': f'Bearer {token}'},
            },
        },
    }


def config_json(url: str, token: str) -> str:
    """The config as text, formatted for a human to read and paste.

    `ensure_ascii=False` because the masked preview's bullet characters would
    otherwise serialize as `\\u2022` escapes and the on-page preview would read
    `Bearer \\u2022\\u2022…`. JSON is UTF-8 by default (RFC 8259), so emitting
    them raw is correct for the downloaded file too."""
    return json.dumps(config_entry(url, token), indent=2, ensure_ascii=False)


def preview_token(token: str) -> str:
    """A bounded, obviously-masked stand-in for the token, for the on-page
    preview. Bounded because the shipped compose sets the token to APP_SEED,
    and a mask as long as the secret both looks broken and leaks its length."""
    token = token or ''
    if not token:
        return ''
    return '•' * 8 + (token[-4:] if len(token) > 4 else '')


def preview_json(url: str, token: str) -> str:
    """The same config with the token masked — safe to render in HTML."""
    return config_json(url, preview_token(token))


def claude_code_command(url: str, token: str) -> str:
    """The equivalent one-liner for the Claude Code CLI.

    No mcp-remote here: Claude Code speaks HTTP MCP natively, so this is a
    direct registration. Spaces are safe in a shell command, so the header takes
    its conventional form."""
    return (f'claude mcp add --transport http {SERVER_KEY} {url} '
            f'--header "Authorization: Bearer {token}"')


def preview_claude_code_command(url: str, token: str) -> str:
    return claude_code_command(url, preview_token(token))
