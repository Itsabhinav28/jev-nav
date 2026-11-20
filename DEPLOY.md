# Deploying jev-nav on one GCP VM (1 container, 1 URL)

Everything below is copy-pasteable. Replace the placeholders in `UPPER_CASE` (`APP_HOST`, `PROJECT_ID`, ...). Secrets are never in git: you type them into `/opt/jev-nav/.env` on the VM yourself.

## 0. What you get

```
 phone   https://APP_HOST/phone?k=SECRET --HTTPS-->  +------- Compute Engine VM (Ubuntu 22.04) -------+
 laptop  https://APP_HOST/?k=SECRET      --HTTPS-->  | nginx :443  (TLS + secret-link check)         |
                                                     |   -> 127.0.0.1:8081 -> container "jev"        |
 phone <==== WebRTC video (UDP) ===================> |      (dashboard + Jev + ORB-SLAM3 on 5555)    |
        (direct to the VM, ephemeral UDP ports)      |  host networking                              |
                                                     |  jev --> TypeSafe Jev API, OpenRouter (HTTPS) |
                                                     +-----------------------------------------------+
```

* **One container = one phone at a time.** The server keeps a single global stream state (one camera, one Jev session, one SLAM map). A second phone connecting at the same time would clash with the first. That is the design limit of this deployment.
* **No password.** Access is by a secret link: people open `https://APP_HOST/?k=SECRET` once, nginx sets a cookie, and they stay in for a year. Anyone without the link (or the cookie) gets 403. Rotate the secret any time (section 7) to cut everyone off.
* The app itself has **no login**. Never expose the container ports directly (see Security).
* Jev image input is experimental (see the main README); treat navigation output as assistive only.

## 1. Prerequisites

- [ ] GCP project with billing enabled
- [ ] `gcloud` CLI (optional, you can use the Cloud Console instead)
- [ ] A domain where you can create one DNS A record (e.g. `app.example.com`)
- [ ] Write access to `github.com/Itsabhinav28/jev-nav` (to add a deploy key)
- [ ] API keys in hand: TypeSafe (`TYPESAFE_API_KEY`), OpenRouter (`OPENROUTER_API_KEY`)

## 2. Capacity and sizing (ESTIMATES, not measurements)

One instance: Python server + about 2 frames/s sent to Jev + ffmpeg/WebRTC decode + ORB-SLAM3 (up to 15 fps at 640 px, about 70 ms/frame, i.e. roughly one CPU core busy) + the ORB vocabulary (about 1-2 GB RAM). Roughly **1.5-2 CPU cores and 3-4 GB RAM** while a phone streams, plus the OS/nginx/Docker.

| Machine type | vCPU / RAM | Approx. on-demand, us-central1, 24x7 | Verdict |
|---|---|---|---|
| **e2-standard-4** | 4 / 16 GB | ~US$98/month | **Recommended** for one stream, with headroom for the image build. |
| e2-standard-2 | 2 / 8 GB | ~US$49/month | Possible but tight: SLAM and the build compete for 2 cores. |

Add roughly US$6/month for a 50-60 GB balanced disk. Prices change and vary by region: check the [GCP pricing calculator](https://cloud.google.com/products/calculator). Compose caps the container at 8 GB RAM (`mem_limit`). After first real use, check `docker stats` and resize if needed.

**Save money:** stop the VM when idle (`gcloud compute instances stop jev-nav --zone $ZONE`, or Console -> Stop). Compute billing stops; disk and static IP still bill. The container restarts on boot (`restart: unless-stopped`).

## 3. Create the GCP resources

You can use the Console (VM instances -> Create instance) with these values, or gcloud:

```bash
export PROJECT_ID=your-project-id
export REGION=us-central1
export ZONE=us-central1-a
export YOUR_IP=$(curl -s https://ifconfig.me)   # your current public IP

gcloud config set project $PROJECT_ID
gcloud services enable compute.googleapis.com

# static external IP (note the printed address)
gcloud compute addresses create jev-nav-ip --region $REGION
gcloud compute addresses describe jev-nav-ip --region $REGION --format='value(address)'

# VM (network tag "jev-nav" must match the firewall rules below)
gcloud compute instances create jev-nav \
  --zone $ZONE \
  --machine-type e2-standard-4 \
  --image-family ubuntu-2204-lts --image-project ubuntu-os-cloud \
  --boot-disk-size 60GB --boot-disk-type pd-balanced \
  --address jev-nav-ip \
  --tags jev-nav
```

### Firewall

```bash
# web (nginx + Let's Encrypt validation)
gcloud compute firewall-rules create jev-nav-web --direction INGRESS \
  --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0 --target-tags jev-nav

# SSH only from your IP
gcloud compute firewall-rules create jev-nav-ssh --direction INGRESS \
  --allow tcp:22 --source-ranges ${YOUR_IP}/32 --target-tags jev-nav

# WebRTC media (UDP)
gcloud compute firewall-rules create jev-nav-webrtc --direction INGRESS \
  --allow udp:32768-60999 --source-ranges 0.0.0.0/0 --target-tags jev-nav

# the default network ships a rule that allows SSH from anywhere; remove it
gcloud compute firewall-rules delete default-allow-ssh
```

Why that UDP range: aiortc/aioice binds its ICE sockets with `local_addr=(address, 0)` (see `aioice/ice.py`), so the OS picks an ephemeral port. On Linux that is `net.ipv4.ip_local_port_range`, default 32768-60999, so that range must be open. Trade-off: a wide UDP range is reachable on the VM. Only the app's WebRTC sockets listen there, but it is wider than ideal. Hardening upgrade (optional): run coturn as a TURN server on a single fixed port and force relay.

If SSH stops working because your home IP changed: `gcloud compute firewall-rules update jev-nav-ssh --source-ranges NEW_IP/32`.

### DNS

At your DNS provider create **one A record** pointing at the static IP (DNS only, no proxy/CDN):

```
APP_HOST  A  STATIC_IP        # e.g. app.example.com
```

Check: `dig +short APP_HOST` prints the IP. Finish this before step 8.

## 4. SSH in

```bash
gcloud compute ssh jev-nav --zone $ZONE      # or the SSH button in the Console
```

```bash
sudo apt-get update && sudo apt-get install -y git
sudo mkdir -p /opt/jev-nav && sudo chown $USER:$USER /opt/jev-nav
```

## 5. GitHub access over SSH, clone, then bootstrap the OS

```bash
ssh-keygen -t ed25519 -C "jev-nav-gcp-vm" -f ~/.ssh/jev_deploy -N ""
cat ~/.ssh/jev_deploy.pub
```

Copy the printed public key. In GitHub: repo **Itsabhinav28/jev-nav** -> Settings -> Deploy keys -> Add deploy key -> title `gcp-vm`, paste, leave "Allow write access" **unchecked** (read-only).

```bash
cat >> ~/.ssh/config <<'CFG'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/jev_deploy
  IdentitiesOnly yes
CFG
chmod 600 ~/.ssh/config

ssh -T git@github.com    # type "yes" for the host key; expect "successfully authenticated"
git clone git@github.com:Itsabhinav28/jev-nav.git /opt/jev-nav
bash /opt/jev-nav/deploy/scripts/setup-vm.sh
exit     # log out ...
```
```bash
gcloud compute ssh jev-nav --zone $ZONE     # ... and back in so the docker group applies
```

If the folder already exists from an earlier clone: `cd /opt/jev-nav && git checkout main && git pull --ff-only origin main`.

`setup-vm.sh` installs: system updates, Docker Engine + compose plugin (official Docker apt repo), nginx, certbot (snap), unattended security upgrades, a 4 GB swapfile, and makes sure `/opt/jev-nav` is yours.

## 6. Secrets file (not in git)

```bash
cp /opt/jev-nav/deploy/env.example /opt/jev-nav/.env
chmod 600 /opt/jev-nav/.env
nano /opt/jev-nav/.env     # set APP_HOST, LETSENCRYPT_EMAIL, TYPESAFE_API_KEY, OPENROUTER_API_KEY
```

`APP_HOST` is the bare hostname (no `https://`), e.g. `app.example.com`. `ICE_STUN_URLS` is required in the cloud: the VM only knows its private IP, so STUN lets the server advertise its public address to the phone. Keep the default unless you run your own STUN.

OpenRouter note: audio (TTS) needs a balance of at least about US$0.50; below that OpenRouter returns HTTP 402 and only speech fails, other features keep working.

## 7. The secret link (replaces the password)

```bash
set -a; . /opt/jev-nav/.env; set +a
bash /opt/jev-nav/deploy/scripts/make-access-key.sh "$APP_HOST" --no-reload
```

It prints two links: the dashboard link and the phone link, each ending in `?k=SECRET`. Send the phone link to whoever should use it. The first open sets a cookie; after that the plain URL works in that browser. The key is stored in `/etc/nginx/jev-access.conf` (mode 640, never in git).

**Rotate (cut everyone off):** run the same script again (without `--no-reload` once nginx is set up). The old link and all old cookies stop working at once; send the new link.

Anyone who has the link can use your TypeSafe/OpenRouter quota, so share it only with people you trust and set spending limits on the provider side.

## 8. TLS certificate and nginx

The certificate is created first with the standalone method (nginx stopped for a few seconds); the hooks are saved so automatic renewals work.

```bash
dig +short "$APP_HOST"                    # must print the VM's static IP, else stop here

sudo systemctl stop nginx
sudo certbot certonly --standalone \
  -d "$APP_HOST" \
  -m "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"

bash /opt/jev-nav/deploy/scripts/render-nginx.sh "$APP_HOST" --no-reload
sudo systemctl start nginx

sudo certbot renew --dry-run             # auto-renew test (a systemd timer does the real renewals)
```

`render-nginx.sh` refuses to run until step 7 has created the access key.

## 9. Build the image and start the container

```bash
sudo apt-get install -y tmux && tmux new -s build      # the first build is long; tmux survives an SSH drop
cd /opt/jev-nav/deploy
docker compose --env-file /opt/jev-nav/.env build     # first build is slow (ORB-SLAM3 compiles): 20-40 min
docker compose --env-file /opt/jev-nav/.env up -d
docker compose --env-file /opt/jev-nav/.env ps        # wait until it is (healthy), 1-2 min
```

Reattach after a dropped SSH: `tmux attach -t build`. Shortcut so you can drop `--env-file` later: `echo 'export COMPOSE_ENV_FILES=/opt/jev-nav/.env' >> ~/.bashrc && . ~/.bashrc`

## 10. Verify

```bash
curl -s http://127.0.0.1:8081/healthz; echo                                     # ok
curl -s https://$APP_HOST/healthz; echo                                         # ok (open by design)
curl -s -o /dev/null -w '%{http_code}\n' https://$APP_HOST/                     # 403: no key
curl -s -o /dev/null -w '%{http_code}\n' "https://$APP_HOST/?k=SECRET"          # 200
```

* Dashboard (laptop browser): the dashboard link from step 7.
* Phone: the phone link from step 7, allow the camera. The certificate is a real Let's Encrypt one, so there is no browser warning.
* Logs: `docker compose --env-file /opt/jev-nav/.env logs -f --tail=100 jev`; nginx: `sudo tail -f /var/log/nginx/error.log`

## 11. Updating and routine operations

```bash
bash /opt/jev-nav/deploy/scripts/deploy.sh     # git pull --ff-only, build, up -d, wait for health
```

Rollback (the script keeps the previous image as `jev-nav:previous`):

```bash
docker tag jev-nav:previous jev-nav:latest
cd /opt/jev-nav/deploy && docker compose --env-file /opt/jev-nav/.env up -d --no-build
```

| Task | Command |
|---|---|
| Restart the app | `docker compose --env-file /opt/jev-nav/.env restart jev` |
| Status / resources | `docker compose --env-file /opt/jev-nav/.env ps`; `docker stats --no-stream` |
| Disk cleanup | `docker system prune -af`; `df -h /` |
| Apply `.env` changes | edit, then `docker compose --env-file /opt/jev-nav/.env up -d` |
| New secret link | `bash /opt/jev-nav/deploy/scripts/make-access-key.sh "$APP_HOST"` |
| Stop VM when idle | `gcloud compute instances stop jev-nav --zone $ZONE` |
| Start VM | `gcloud compute instances start jev-nav --zone $ZONE` (static IP is kept) |

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Phone connects, page loads, but no video | UDP blocked or no public ICE candidate. Check firewall rule `jev-nav-webrtc` (udp:32768-60999, VM has tag `jev-nav`) and that `ICE_STUN_URLS` is set (`docker exec jev env \| grep ICE`). Some mobile carriers and corporate Wi-Fi block UDP; try another network. A TURN server is the real fix. |
| 403 "Access link required." | Open the link with `?k=SECRET`. If it was rotated, ask for the new link. Cookies are per browser. |
| Browser certificate error | DNS did not point at the IP when certbot ran, or cert expired: `sudo certbot certificates`, `sudo certbot renew --dry-run`. |
| 502 Bad Gateway | Container down/unhealthy: `docker compose ... ps`, `logs jev`; test `curl http://127.0.0.1:8081/healthz`. |
| nginx fails `-t` | Cert missing under `/etc/letsencrypt/live/APP_HOST/` (step 8 failed) or `/etc/nginx/jev-access.conf` missing (step 7). |
| SLAM "lost" / no map | Needs steady motion and textured scenes; check the container logs for SLAM start-up errors. |
| No audio; log shows OpenRouter 402 | OpenRouter balance is under about US$0.50 for TTS; top up. Everything else still works. |
| Container restarts / killed | Out of memory: `docker inspect jev --format '{{.State.OOMKilled}}'`, `free -h`, `sudo dmesg \| grep -i oom`. Use a bigger machine type or raise `mem_limit`. |
| Phone camera will not start | The page must be HTTPS with a valid cert (nginx provides it) and the camera permission must be granted. |
| A second phone cannot stream at the same time | By design: one container serves one phone at a time. |

## 13. Security notes

- The app has no authentication. Do not open the container ports (8081, 8443, 5555) in the GCP firewall: with host networking the VM firewall is the boundary. Only tcp 80/443, SSH from your IP, and the WebRTC UDP range are open.
- The secret link is a bearer secret: whoever has it has access (and can spend your API quota). It travels over HTTPS only (HTTP redirects to HTTPS), but it can appear in browser history and server logs. Rotate it when someone leaves or if it leaks.
- Keep `/opt/jev-nav/.env` at mode 600 and out of git; rotate API keys if leaked. The GitHub deploy key is read-only.
- Unattended security upgrades are on; reboot occasionally (`sudo reboot`; the container auto-starts).
