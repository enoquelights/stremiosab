# Stremio + SABnzbd disposable streaming add-on

A small, local Stremio add-on that searches Prowlarr, submits one selected Usenet result to SABnzbd, serves the completed video over HTTP with byte-range support, and asks SABnzbd to delete its job files after playback reaches the configured watched threshold or after expiry.

**Use only with indexers and media you are authorised to access, download and view.** This is a local-network service; do not expose it to the public internet.

## Important behaviour / Stremio protocol limitation

Stremio requests an add-on's `/stream/...` resource when the detail page is opened, before the viewer necessarily presses Play. This implementation therefore queues the **first Prowlarr result** when Stremio asks for streams, and returns one stream URL. It does not wait for the Play click to queue the download, because a normal Stremio add-on does not provide a reliable "user selected this one stream" callback before the player opens the URL.

That means opening a title's detail page may start a download. The app deliberately queues only one result, not every search result. Check the release title shown by Stremio before relying on it.

The stream URL waits for SABnzbd to finish and unpack the job, then serves the video. Large downloads may take longer than some Stremio clients tolerate while waiting for the HTTP response. For best results, open the title page, allow the download to finish, then select the stream.

Player events are correlated by Stremio video ID. To avoid ambiguity, the implementation allows only one outstanding session per video ID. Player stop at or above 90% schedules immediate cleanup. Otherwise the expiry is 12 hours after the last pause/stop event or completion. The SABnzbd history API is used to delete the specific job and its files.

## Requirements

- Docker Engine + Docker Compose plugin on a Linux mini PC.
- Existing SABnzbd reachable from this container.
- Existing Prowlarr reachable from this container, with at least one Usenet indexer configured.
- SABnzbd configured with a category named `stremio` whose folder is `stremio`.
- A shared host path: the SABnzbd container must mount the host completed-download directory as `/downloads/complete`; this add-on mounts that exact same host directory read-only at `/downloads/complete`.
- The video job folder must be created under `/downloads/complete/stremio/`.

## 1. Prepare SABnzbd

In SABnzbd:
1. Open **Config → Categories**.
2. Add category `stremio`.
3. Set its Folder/Path to `stremio`.
4. Keep post-processing/unpacking enabled.
5. Note the API key under **Config → General**.
6. Ensure the completed download directory is mounted into the SAB container at `/downloads/complete`.

Example SAB container volume (adapt your existing Compose; do not replace your working SAB config volume):
```yaml
volumes:
  - /srv/downloads/complete:/downloads/complete
```

Set SAB's completed-download directory to `/downloads/complete` inside its container. The category then creates content below `/downloads/complete/stremio`.

## 2. Prepare Prowlarr

- Add and test your authorised Usenet indexer(s).
- Copy the API key from **Settings → General**.
- The app uses `GET /api/v1/search` with the movie category `2000` or TV category `5000`.
- Prowlarr must be reachable at the URL configured in `.env`.

## 3. Configure the add-on

```bash
mkdir -p /opt/stremio-usenet
cd /opt/stremio-usenet
```

Extract this package here, then:
```bash
cp .env.example .env
```

Edit `.env`:
- `ADDON_BASE_URL`: use the mini PC LAN IP, e.g. `http://192.168.1.50:7000`.
- `PROWLARR_API_KEY`: Prowlarr API key.
- `SAB_API_KEY`: SABnzbd API key.
- `SAB_COMPLETE_HOST_PATH`: exact host path mounted into SAB as `/downloads/complete`.

If Prowlarr or SABnzbd are in Docker containers and not published on the host, `host.docker.internal` may not work for your setup. Put the containers on a shared Docker network and use their container DNS names instead.

Start:
```bash
docker compose up -d --build
docker compose logs -f stremio-usenet
```

Health check:
```bash
curl http://127.0.0.1:7000/health
```

Manifest:
```text
http://MINI-PC-LAN-IP:7000/manifest.json
```

## 4. Install in Stremio

On a device on the same home network:
1. Open Stremio's add-on catalogue / add-on URL installation flow.
2. Enter `http://MINI-PC-LAN-IP:7000/manifest.json`.
3. Install the local add-on.
4. Open a movie or an episode that uses an IMDb `tt...` ID.
5. The add-on searches Prowlarr and queues the first Usenet result in SABnzbd.
6. Allow SABnzbd to finish and unpack. Select the returned `SABnzbd · temporary` stream.

The Stremio device must be able to reach the mini PC at the LAN URL. Do not use `localhost` in `ADDON_BASE_URL` unless Stremio itself runs on the mini PC.

## 5. Verify cleanup

Watch a supported title past 90% and stop playback. Stremio should send a player `stop` event; the add-on marks the session for cleanup and calls SABnzbd's history-delete API with `del_files=1`.

If playback stops before 90%, the job expires after 12 hours. The expiry is checked once per minute.

Inspect:
```bash
docker compose logs -f stremio-usenet
```

## 6. Security and operational notes

- Do not port-forward this service, Prowlarr, or SABnzbd.
- Keep it on a trusted LAN. This initial version has no user authentication; any device able to reach it can request a download.
- Back up `./state` if you want session diagnostics, but it contains no media.
- The app mounts the completed-download directory read-only. File deletion is delegated to SABnzbd's API for the exact NZB job.
- The first version returns one Prowlarr result and supports a single main video file per NZB job. Multi-episode packs, password-protected archives, and multiple video files need a selection UI and are not handled.
- If the installed Stremio client does not send player events, the 12-hour expiry still applies.
- This is a starter implementation, not a hardened multi-user service. Test with a small, lawful sample before relying on automatic cleanup.

## Troubleshooting

### No streams returned
- Check Prowlarr URL and API key.
- Confirm a Usenet indexer is enabled and returns results.
- Check that the IMDb item is available through Cinemeta.
- Inspect app logs.

### SAB job completes but playback never starts
- Confirm SAB category `stremio` has folder `stremio`.
- Confirm the SAB complete path and `SAB_COMPLETE_HOST_PATH` refer to the same host directory.
- Confirm the completed job folder appears under `/downloads/complete/stremio/`.
- Check the SAB history record and app logs.

### Player cannot connect
- `ADDON_BASE_URL` must be the mini PC's LAN IP, not `localhost` or a Docker-only hostname.
- Allow TCP port 7000 on the mini PC firewall for the home LAN.
- Check that the client can open `http://MINI-PC-LAN-IP:7000/health`.

### Cleanup does not happen
- Confirm Stremio player events reach `/player/...` in the logs (enable DEBUG logging if needed).
- Confirm the SAB API key has sufficient access.
- Check that SAB history retains completed jobs until the add-on deletes them.
