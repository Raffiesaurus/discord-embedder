# Discord Embedder

Watches every message, converts links from common social sites to cleaner mirror URLs, reposts, and deletes the original. Attachments are preserved.

> This is a private project I made for me and my friends. If you want to use it, please create your own Discord bot and run it on your own server.

## What it does

* Detects links in any message
* Rewrites to better-embed mirrors
* Reposts with masked “Original” and “Embed” labels
* Carries over attachments
* Resolves Instagram /share/* to canonical /p|/reel|/tv/... before mirroring

## Demo
https://github.com/user-attachments/assets/a08744c2-5e67-4844-99a6-89147e299a36


https://github.com/user-attachments/assets/cdda6e67-852a-4b55-a9f5-808339c6fcfb


https://github.com/user-attachments/assets/237f2456-a0bf-469a-9891-78c495be8104



## Mirrors used

* Twitter/X → fixupx.com
* Instagram → kkinstagram.com
* Reddit → rxddit.com
* TikTok → vxtiktok.com
* Bluesky → bskx.app

You can edit the map in DEFAULT_MIRRORS inside bot.py.

## Requirements
* Python 3.10+
* A Discord bot token in .env as DISCORD_TOKEN=...
* Message Content intent enabled for your app in the Developer Portal and in code (intents.message_content = True). 

## Setup (self-host)

This repo is not a hosted service. If you want to use it, make your own bot and run it yourself.

* Create a Discord application and bot, then get your token. 
* Enable the Message Content privileged intent for the app. 
* Clone the repo, create a virtualenv, install deps, and set your .env:

## Configuration
.env
```
DISCORD_TOKEN=your_bot_token
```

## How it works

* Regex finds URLs in the message.
* If the host is known, swap to the mirror. For Instagram: 
  * resolve /share/* to canonical
  * skip stories

## Notes / limitations
* Only public content embeds cleanly.
* If a site changes its markup or blocks fetching, the bot still posts the mirrored link.
* This project doesn’t store messages; it just reads, rewrites, and reposts.

## Contributing
No public support. Fork it and make your own tweaks.

## License
Personal project. If you fork, add your own license file.
