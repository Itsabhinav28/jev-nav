# Deploying jev-nav on one GCP VM (3 concurrent users)

Everything below is copy-pasteable. Replace the placeholders in `UPPER_CASE` (`DOMAIN`, `PROJECT_ID`, ...). Secrets are never in git: you type them into `/opt/jev-nav/.env` on the VM yourself.

## 0. What you get

```
 phone  https://u1.DOMAIN/phone  --HTTPS-->  +---------- Compute Engine VM (Ubuntu 22.04) -----------------------+
 laptop https://u1.DOMAIN/       --HTTPS-->  | nginx :443  (TLS + basic auth)                                   |
 (same for u2.DOMAIN, u3.DOMAIN)             |   u1 -> 127.0.0.1:8081 -> container jev1 (SLAM on 5555)         |
                                             |   u2 -> 127.0.0.1:8082 -> container jev2 (SLAM on 5556)         |
 phone <==== WebRTC video (UDP) ===========> |   u3 -> 127.0.0.1:8083 -> container jev3 (SLAM on 5557)         |
        (direct to VM, ephemeral UDP ports)  |  containers use host networking                                  |
                                             |  jev1..3 --> TypeSafe Jev API, OpenRouter API (outbound HTTPS)   |
                                             +------------------------------------------------------------------+
```

* The server keeps ONE global stream state, so one container = one phone at a time. Three containers = three independent users. There is deliberately no 4th slot.
* Each instance has its own subdomain (the pages use absolute URLs like `/offer`, so path prefixes cannot work).
* The app has **no login of its own**. nginx HTTP basic auth is **mandatory** on a public IP.
* Jev image input is experimental (see the main README); treat navigation output as assistive only.

## 1. Prerequisites

- [ ] GCP project with billing enabled
- [ ] `gcloud` CLI installed and logged in: `gcloud auth login`
- [ ] A domain where you can create DNS A records
- [ ] Write access to `github.com/Itsabhinav28/jev-nav` (to add a deploy key)
- [ ] API keys in hand: TypeSafe (`TYPESAFE_API_KEY`), OpenRouter (`OPENROUTER_API_KEY`)

## 2. Capacity and sizing (ESTIMATES, not measurements)

Per instance: Python server + about 2 frames/s sent to Jev + ffmpeg/WebRTC decode + ORB-SLAM3 (up to 15 fps at 640 px, about 70 ms/frame, i.e. roughly one CPU core busy) + the ORB vocabulary (about 1-2 GB RAM per SLAM process).

| Item | Per instance | x3 |
|---|---|---|
| CPU | ~1.5-2 cores busy while a phone streams | ~5-6 cores |
| RAM | ~3-4 GB (SLAM 1-2 GB, Python/ffmpeg/buffers 1-2 GB) | ~9-12 GB, plus ~1.5 GB for OS/nginx/Docker |
| Disk | image (SLAM + Python deps) several GB, logs, `runs/` output | 50-60 GB boot disk |

| Machine type | vCPU / RAM | Approx. on-demand, us-central1, 24x7 | Verdict |
|---|---|---|---|
| e2-standard-4 | 4 / 16 GB | ~US$98/month | RAM fits, but CPU is oversubscribed when all 3 stream at once; SLAM will lag. Acceptable only if usually 1-2 users. |
| **e2-standard-8** | 8 / 32 GB | ~US$196/month | **Recommended**: headroom for 3 simultaneous streams. |

Add roughly US$6/month for a 60 GB balanced disk. Prices change and vary by region: check the [GCP pricing calculator](https://cloud.google.com/products/calculator). Compose caps each container at 7 GB RAM (`mem_limit`) so one runaway instance cannot take down the others. After first real use, check `docker stats` and resize if needed.

**Save money:** stop the VM when idle (`gcloud compute instances stop jev-nav --zone $ZONE`). Compute billing stops; disk and static IP still bill. Containers restart on boot (`restart: unless-stopped`).

## 3. Create the GCP resources (run on your laptop)

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

# VM
gcloud compute instances create jev-nav \
  --zone $ZONE \
  --machine-type e2-standard-8 \
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

Why that UDP range: aiortc/aioice binds its ICE sockets with `local_addr=(address, 0)` (see `aioice/ice.py`), so the OS picks an ephemeral port. On Linux that is `net.ipv4.ip_local_port_range`, default 32768-60999, so that range must be open. Trade-off: a wide UDP range is reachable on the VM. Only the app's WebRTC sockets listen there, but it is wider than ideal. Hardening upgrade (optional): run coturn as a TURN server on a single fixed port and force relay; that narrows the range to one port but needs app/ICE configuration changes beyond this guide.

If SSH stops working because your home IP changed: `gcloud compute firewall-rules update jev-nav-ssh --source-ranges NEW_IP/32`.

### DNS

At your DNS provider create three **A records** pointing to the static IP:

```
u1.DOMAIN  A  STATIC_IP
u2.DOMAIN  A  STATIC_IP
u3.DOMAIN  A  STATIC_IP
```

Check: `dig +short u1.DOMAIN u2.DOMAIN u3.DOMAIN` prints the IP three times. Finish this before step 8.

## 4. SSH in

```bash
gcloud compute ssh jev-nav --zone $ZONE
```

On the VM, create the app directory (the bootstrap script needs the repo, which comes next):

```bash
sudo apt-get update && sudo apt-get install -y git
sudo mkdir -p /opt/jev-nav && sudo chown $USER:$USER /opt/jev-nav
```

## 5. GitHub access over SSH, clone, then bootstrap the OS

Generate a key on the VM:

```bash
ssh-keygen -t ed25519 -C "jev-nav-gcp-vm" -f ~/.ssh/jev_deploy -N ""
cat ~/.ssh/jev_deploy.pub
```

Copy the printed public key. In GitHub: repo **Itsabhinav28/jev-nav** -> Settings -> Deploy keys -> Add deploy key -> title `gcp-vm`, paste, leave "Allow write access" **unchecked** (read-only). Alternative: add it under your account (Settings -> SSH and GPG keys); that grants access to all your repos, so the deploy key is safer.

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
git clone -b deploy/gcp-vm git@github.com:Itsabhinav28/jev-nav.git /opt/jev-nav
```

(`-b deploy/gcp-vm` until that branch is merged; afterwards drop it.) Now bootstrap the OS (idempotent; handles no secrets):

```bash
bash /opt/jev-nav/deploy/scripts/setup-vm.sh
exit     # log out ...
```
```bash
gcloud compute ssh jev-nav --zone $ZONE     # ... and back in so the docker group applies
```

`setup-vm.sh` installs: system updates, Docker Engine + compose plugin (official Docker apt repo), nginx, certbot (from snap, the method certbot itself recommends and newer than Ubuntu's apt package), apache2-utils (`htpasswd`), unattended security upgrades, a 4 GB swapfile, and makes sure `/opt/jev-nav` is yours.

## 6. Secrets file (not in git)

```bash
cp /opt/jev-nav/deploy/env.example /opt/jev-nav/.env
chmod 600 /opt/jev-nav/.env
nano /opt/jev-nav/.env     # set DOMAIN, LETSENCRYPT_EMAIL, TYPESAFE_API_KEY, OPENROUTER_API_KEY
```

`ICE_STUN_URLS` is required in the cloud: the VM only knows its private IP, so STUN lets the server advertise its public address to the phone. Keep the default unless you run your own STUN.

OpenRouter note: audio (TTS) needs a balance of at least about US$0.50; below that OpenRouter returns HTTP 402 and only speech fails, other features keep working.

Optional, Google Secret Manager instead of typing keys on the VM (the VM service account needs `roles/secretmanager.secretAccessor`):

```bash
gcloud services enable secretmanager.googleapis.com
printf '%s' 'THE_KEY' | gcloud secrets create typesafe-api-key --data-file=-
# on the VM: fetch and write it into .env (compose still reads /opt/jev-nav/.env)
KEY=$(gcloud secrets versions access latest --secret=typesafe-api-key)
sed -i "s|^TYPESAFE_API_KEY=.*|TYPESAFE_API_KEY=$KEY|" /opt/jev-nav/.env
```

## 7. Basic-auth users

```bash
sudo htpasswd -c /etc/nginx/.htpasswd user1     # -c only the first time (creates the file)
sudo htpasswd /etc/nginx/.htpasswd user2
sudo htpasswd /etc/nginx/.htpasswd user3
sudo chown root:www-data /etc/nginx/.htpasswd && sudo chmod 640 /etc/nginx/.htpasswd
```

All three subdomains share this file. For separate logins per instance, make copies and point each server block at its own `auth_basic_user_file`. A commented IP-allowlist alternative is in `deploy/nginx/jev-proxy.inc`.

## 8. TLS certificate and nginx

One certificate covers all three subdomains (certbot stores it under `u1.DOMAIN`, which the config expects). It is created first with the standalone method (nginx stopped for a few seconds); the hooks are saved so automatic renewals work.

```bash
set -a; . /opt/jev-nav/.env; set +a      # loads DOMAIN and LETSENCRYPT_EMAIL

sudo systemctl stop nginx
sudo certbot certonly --standalone \
  -d u1.$DOMAIN -d u2.$DOMAIN -d u3.$DOMAIN \
  -m "$LETSENCRYPT_EMAIL" --agree-tos --no-eff-email \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"

# render config for your domain, install, run nginx -t
bash /opt/jev-nav/deploy/scripts/render-nginx.sh "$DOMAIN" --no-reload
sudo systemctl start nginx

sudo certbot renew --dry-run             # auto-renew test (a systemd timer does the real renewals)
```

Manual equivalent of the render script: `sed "s/DOMAIN/example.com/g" deploy/nginx/jev-nav.conf | sudo tee /etc/nginx/sites-available/jev-nav.conf`, plus copy `deploy/nginx/jev-proxy.inc` to `/etc/nginx/snippets/jev-proxy.conf`, symlink into `sites-enabled`, `sudo nginx -t`, reload.

## 9. Build the image and start the 3 instances

```bash
cd /opt/jev-nav/deploy
docker compose --env-file /opt/jev-nav/.env build     # first build is slow (ORB-SLAM3)
docker compose --env-file /opt/jev-nav/.env up -d
docker compose --env-file /opt/jev-nav/.env ps        # wait until all three are (healthy)
```

Shortcut so you can drop `--env-file` later: `echo 'export COMPOSE_ENV_FILES=/opt/jev-nav/.env' >> ~/.bashrc && . ~/.bashrc`

## 10. Verify

```bash
for p in 8081 8082 8083; do curl -s http://127.0.0.1:$p/healthz; echo; done     # ok, ok, ok
curl -sI https://u1.$DOMAIN/ | head -1                                           # 401: auth required
curl -s -u user1:PASSWORD -o /dev/null -w '%{http_code}\n' https://u1.$DOMAIN/   # 200
curl -s https://u1.$DOMAIN/healthz                                               # ok (no auth, by design)
```

* Dashboard (laptop browser): `https://u1.DOMAIN/`, `https://u2.DOMAIN/`, `https://u3.DOMAIN/`
* Phone: open `https://u1.DOMAIN/phone`, enter the basic-auth login, allow the camera. Each person uses a different instance. The certificate is a real Let's Encrypt one, so there is no browser warning.
* Logs: `docker compose --env-file /opt/jev-nav/.env logs -f --tail=100 jev1`; nginx: `sudo tail -f /var/log/nginx/error.log`

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
| Restart one instance | `docker compose --env-file /opt/jev-nav/.env restart jev2` |
| Status / resources | `docker compose --env-file /opt/jev-nav/.env ps`; `docker stats --no-stream` |
| Disk cleanup | `docker system prune -af`; `df -h /` |
| Apply `.env` changes | edit, then `docker compose --env-file /opt/jev-nav/.env up -d` |
| Stop VM when idle | `gcloud compute instances stop jev-nav --zone $ZONE` |
| Start VM | `gcloud compute instances start jev-nav --zone $ZONE` (static IP is kept) |

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Phone connects, page loads, but no video | UDP blocked or no public ICE candidate. Check firewall rule `jev-nav-webrtc` (udp:32768-60999, VM has tag `jev-nav`) and that `ICE_STUN_URLS` is set (`docker exec jev1 env \| grep ICE`). Some mobile carriers and corporate Wi-Fi block UDP; try another network. A TURN server is the real fix. |
| Browser certificate error | DNS did not point at the IP when certbot ran, or cert expired: `sudo certbot certificates`, `sudo certbot renew --dry-run`. |
| 502 Bad Gateway | Container down/unhealthy: `docker compose ... ps`, `logs jev1`; test `curl http://127.0.0.1:8081/healthz`. |
| Cannot log in (401) | Recreate the user: `sudo htpasswd /etc/nginx/.htpasswd user1`. |
| nginx fails `-t` | Cert missing under `/etc/letsencrypt/live/u1.DOMAIN/`: step 8 certbot failed. |
| SLAM "lost" / no map | Needs steady motion and textured scenes; look at the container logs for SLAM start-up errors and confirm SLAM ports are unique per instance (5555/5556/5557). |
| No audio; log shows OpenRouter 402 | OpenRouter balance is under about US$0.50 for TTS; top up. Everything else still works. |
| Container restarts / killed | Out of memory: `docker inspect jev1 --format '{{.State.OOMKilled}}'`, `free -h`, `sudo dmesg \| grep -i oom`. Use a bigger machine type or raise `mem_limit`. |
| Phone camera will not start | The page must be HTTPS with a valid cert (nginx provides it) and the camera permission must be granted. |
| A 4th person cannot connect | By design there are only 3 instances. |

## 13. Security notes

- The app has no authentication. Do not open the container ports (8081-8083, 8443-8445, 5555-5557) in the GCP firewall: with host networking the VM firewall is the boundary. Only tcp 80/443, SSH from your IP, and the WebRTC UDP range are open.
- Keep `/opt/jev-nav/.env` at mode 600 and out of git; rotate keys if leaked. The GitHub deploy key is read-only.
- Basic auth is only safe over HTTPS (HTTP redirects to HTTPS). Use long passwords, or the IP allowlist.
- Anyone with a login can spend your TypeSafe/OpenRouter quota; set spending limits on the provider side.
- Unattended security upgrades are on; reboot occasionally (`sudo reboot`; containers auto-start).
