# Discord Embedder

A Discord bot that watches every message, rewrites links from social media sites to cleaner mirror URLs that actually embed in Discord, reposts with attribution, and deletes the original. Attachments are preserved throughout.

> ⚠️ This is a personal project built for a private server. It's not a hosted service - you'll need to run your own bot instance.

---

## Demo

https://github.com/user-attachments/assets/a08744c2-5e67-4844-99a6-89147e299a36

https://github.com/user-attachments/assets/cdda6e67-852a-4b55-a9f5-808339c6fcfb

https://github.com/user-attachments/assets/237f2456-a0bf-469a-9891-78c495be8104

---

## How It Works

1. Bot detects a URL in any message
2. Rewrites the host to a known embed-friendly mirror
3. Reposts the message with **Original** and **Embed** labels
4. Deletes the original message
5. Attachments from the original are carried over

Instagram `/share/*` links are resolved to canonical `/p/`, `/reel/`, or `/tv/` paths before mirroring. Instagram Stories are skipped.

---

## Supported Sites

| Platform | Mirror |
|----------|--------|
| Twitter / X | `fixupx.com` |
| Instagram | `kkinstagram.com` |
| Reddit | `rxddit.com` |
| TikTok | `vxtiktok.com` |
| Bluesky | `bskx.app` |

To add or change mirrors, edit the `DEFAULT_MIRRORS` map in `bot.py`.

---

## Setup

### Requirements

- Python 3.10+
- A Discord bot token with **Message Content** privileged intent enabled

### Steps

1. Create a Discord application and bot at the [Developer Portal](https://discord.com/developers/applications)
2. Enable the **Message Content** privileged intent
3. Clone the repo and install dependencies:
   ```bash
   git clone https://github.com/Raffiesaurus/discord-embedder.git
   cd discord-embedder
   pip install -r requirements.txt
   ```
4. Create a `.env` file:
```
DISCORD_TOKEN=your_bot_token_here
```
5. Run the bot:
```bash
python bot.py
```

---

## Notes

- Only publicly embeddable content will render as a preview
- The bot does not store any message content - it reads, rewrites, and reposts only
- If a mirror site changes its behaviour, the bot will still post the rewritten link - it just may not embed

---

## License

Personal project - no public support. Fork freely and add your own license.
