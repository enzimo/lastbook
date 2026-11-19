# Lastbook as a web service

`server.py` is the same engine as the desktop app, served over HTTP. This page is about running it in a container behind a
reverse proxy (Docker, Traefik, Caddy, nginx) on a domain name, with first-run setup done in the browser. It does not use the
desktop shells (`app/`), the Linux package (`packaging/linux`) or the local AI (`packaging/llm`, `llm_local.py`).

## The image

```bash
docker build --pull -t lastbook:latest -f packaging/docker/Dockerfile .
```

It runs `python3 server.py --data /data/LastbookData --port 8890 --lan` as user 1000:1000 (build arguments `UID` and `GID`),
with the Python packages in `packaging/docker/requirements.txt`. Nothing else is installed.

## Storage

| In the container | What | Notes |
| --- | --- | --- |
| `/data` | the library and setup's own files | Large: Small about 8 GB, Medium about 68 GB, All about 390 GB, and an install or upgrade needs room for the download and the unpacked copy at once. |
| `/data/LastbookData` | the library (`library-data/`, `derived/`) | Setup unpacks a new library to `/data/LastbookData.setup`, then switches by renaming, and keeps the previous one as `/data/LastbookData.old-<date>` until you delete it in Settings. So mount the volume at `/data`, never at `/data/LastbookData`. |
| `/data/settings` | setup's job, a staged switch, and the access key (`lan-key`) | `LASTBOOK_SETTINGS`. Outside the library folder, so the key survives an upgrade. |
| `/index` (optional) | the meaning-based search index | Set `LASTBOOK_INDEX_DIR=/index` and mount a local disk there when `/data` is a network share: the index is SQLite in WAL mode, which needs a local disk. |

The folders must be writable by the container's user (1000:1000 unless built otherwise).

## Settings

| Variable | Default in the image | Meaning |
| --- | --- | --- |
| `LASTBOOK_ALLOWED_HOSTS` | (none) | Host names served besides `localhost` and bare IP addresses, comma-separated, e.g. `lastbook.example.org`. Any other name is refused (DNS rebinding). |
| `LASTBOOK_COOKIE_SECURE` | `1` | The access-key cookie is sent over HTTPS only. Set `0` if the browser reaches the server over plain HTTP. |
| `LASTBOOK_REMOTE_SETUP` | `1` | A browser holding the access key may run setup (Settings, or the first page while there is no library). See below. |
| `LASTBOOK_SETTINGS` | `/data/settings` | Setup's own folder. |
| `LASTBOOK_KEY_FILE` | `$LASTBOOK_SETTINGS/lan-key` | The access key. Delete it and restart to make a new link. |
| `LASTBOOK_INDEX_DIR` | (in the library) | Where the meaning-based search index lives. |
| `LASTBOOK_AI_BASE_URL` | (none) | An OpenAI-compatible server for Chat, with or without `/v1` at the end: `https://ai.example.org/v1`, `http://gateway:8080`. |
| `LASTBOOK_AI_MODEL` | `deepseek-v4.1-flash` | The model name to ask for. It must appear in the server's `/v1/models` list. |
| `LASTBOOK_AI_API_KEY_FILE` / `LASTBOOK_AI_API_KEY` | (none) | Sent as `Authorization: Bearer …`. Prefer the file (a Docker secret). |
| `SURVIVAL_REASONING_EFFORT` | `low` | Sent as `reasoning_effort`. Set it empty for servers that refuse the field. |

## The access link

With `--lan`, everyone except the computer itself needs the access key once; the server then sets a cookie (`HttpOnly`,
`SameSite=Strict`, `Secure`). The server prints the link when it starts:

```
Access link: https://lastbook.example.org/?key=…
```

It is in the container's log (`docker service logs <service>`), and the key alone is in `/data/settings/lan-key`. Keep it
private: whoever has it can read the library and, with `LASTBOOK_REMOTE_SETUP=1`, start and cancel downloads. A reverse proxy
that logs full request addresses also logs the key once per browser.

## Setup in the browser

Without `LASTBOOK_REMOTE_SETUP`, setup only answers requests from the computer itself, as in the desktop app. Behind a proxy
every request comes from the proxy's address, so the web service turns it on for key holders. Such a request still passes
the Host and Origin checks, and it:

- always installs into the server's own data folder (a `folder` in the request is ignored);
- cannot use `use`, `pick` or `remove-ai`, which name or open files on the server;
- is never offered the local AI.

While `/data/LastbookData` holds no library, the server shows only the setup page. When setup finishes, the server restarts
itself, makes the switch, and the page reopens on the library. Later, **Settings** in the sidebar opens the same page to
upgrade, add a state's map or address search, check and repair, or delete the old copy.

Setup's downloader is unchanged: HTTPS only, to `downloads.lastbook.org`, trusting only `engine/setup/roots.pem`.

## Chat

With `LASTBOOK_AI_BASE_URL` set, Chat sends the question and the library passages it found to that server, and only there:
no proxy from the environment, no redirects followed. The status line turns ready once the server lists the model. Without
an AI server, or while it is down or refuses the key, Chat lists the matching library pages instead of an answer.

## Health check

Requests from the container itself need no key, so `GET http://127.0.0.1:8890/` answers 200 once the server is up, whether it
shows the library or setup. The first start of a large library reads its catalog before it answers: allow several minutes.
