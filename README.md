# Discord Embedder

Private bot for a friends server. It watches messages, rewrites social links to
mirrors that Discord can actually embed, and replaces the message with the same
text and attachments so it still looks like the original author sent it.

Discord does not allow bots to edit another user's message. The bot deletes the
original and sends a webhook copy with that user's name and avatar, with only
the URLs rewritten. It does not ping, DM, or react.

In server `763081865455206450`, edits and deletes are posted to channel
`1557109450861453322`. Deletes the bot makes while rewriting a link are not
logged.

## Mirrors

* Twitter/X → `fixupx.com`
* Instagram → `oginstagram.com`
* Reddit → `rxddit.com`
* TikTok → `vxtiktok.com`
* Bluesky → `bskx.app`

Instagram `/share/*` links are resolved to a canonical `/p`, `/reel`, `/reels`,
or `/tv` URL first. TikTok short links (`vm.tiktok.com`, `vt.tiktok.com`) are
resolved the same way. Stories are mirrored; profile-only Instagram URLs are left
alone.

## Requirements

* Python 3.10+
* A Discord bot token
* **Message Content** intent enabled in the Developer Portal
* Channel permissions: View Channel, Send Messages, Embed Links, Attach Files,
  Read Message History, **Manage Messages**, **Manage Webhooks**
* Log channel (`1557109450861453322`): View Channel, Send Messages, Embed Links
* Optional: create a channel webhook yourself (Edit Channel → Integrations →
  Webhooks) if you want to avoid Discord's **APP** badge. Bot-created webhooks
  always show it; Discord does not let apps hide that label.

## Setup

```bash
git clone https://github.com/Raffiesaurus/discord-embedder.git
cd discord-embedder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
DISCORD_TOKEN=your_bot_token
# Optional. Restrict to specific servers:
# ALLOWED_GUILD_IDS=123456789012345678
```

Invite the bot to the server with the permissions above, then either run it
once:

```bash
python bot.py
```

or install it as a systemd service (below).

## Ubuntu systemd service

This assumes the repo lives at `~/discord-embedder` and the venv is `.venv`.

```bash
sudo cp deploy/discord-embedder@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now discord-embedder@$USER
journalctl -u discord-embedder@$USER -f
```

If the project is not in `/home/$USER/discord-embedder`, edit the copied unit
and change `WorkingDirectory`, `EnvironmentFile`, and `ExecStart`.

Useful commands:

```bash
sudo systemctl restart discord-embedder@$USER
sudo systemctl status discord-embedder@$USER
```

## How it works

1. Regex finds URLs in a guild message (DMs are ignored).
2. Known hosts are swapped to their mirror; share/short links are followed first.
3. Tracking query params are stripped.
4. The original wording and attachments are kept, with only the URLs replaced.
5. A webhook re-posts as the author, then the original is deleted. If the
   webhook cannot be used, the original message is left alone.

Edits are handled too: adding a social link later still gets rewritten. That
edit is logged, and the delete from the rewrite is not.

## Edit and delete log

Only server `763081865455206450` is watched. Reports go to channel
`1557109450861453322`.

* An edit shows the channel, author name and id, the old text, the new text,
  and a jump link. Discord embed previews are ignored.
* A delete shows the channel, author, text, and attachment names when the bot
  has seen the message. If it has not (the bot was offline, or the message
  fell out of the cache), the log still notes the channel and message id.
* A bulk delete is one summary, not one post per message.
* Nothing in the log pings anyone.

The bot keeps about 5,000 recent messages from that server in memory so it can
quote them. The cache is cleared when the process stops. Nothing is written
to disk.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Notes

* Only public posts embed reliably.
* If a mirror is down, Discord will show a bad/empty embed; the rewritten link
  is still posted.
* Webhook copies from a **bot-created** webhook show Discord's APP badge; that
  cannot be hidden. Create a webhook in the channel's Integrations settings and
  the bot will use it instead, which usually has no APP label.
* The bot keeps a short in-memory history of one server so the edit and delete
  log can quote messages. It does not write that history to disk, and it never
  pings, DMs, or reacts.

## License

Personal project. If you fork, add your own license file.
