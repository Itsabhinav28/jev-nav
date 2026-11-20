#!/usr/bin/env bash
# One-time (idempotent) VM bootstrap for Ubuntu 22.04. Run as your normal sudo user, NOT root:
#   bash setup-vm.sh
# No secrets are handled here.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then echo "Run as a normal sudo user, not root." >&2; exit 1; fi
APP_DIR=/opt/jev-nav
export DEBIAN_FRONTEND=noninteractive

echo "==> apt update/upgrade + base packages"
sudo apt-get update -y
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl gnupg git ufw htop unzip \
  apache2-utils nginx unattended-upgrades snapd

echo "==> Docker Engine + compose plugin (official Docker apt repo)"
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  # shellcheck disable=SC1091
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -y
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "==> certbot (snap: the method recommended by certbot, newer than the apt package)"
if ! command -v certbot >/dev/null 2>&1; then
  sudo snap install core
  sudo snap refresh core
  sudo snap install --classic certbot
  sudo ln -sf /snap/bin/certbot /usr/bin/certbot
fi

echo "==> nginx"
sudo systemctl enable --now nginx

echo "==> unattended security upgrades"
echo 'APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";' | sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null

echo "==> 4 GB swapfile (safety net against OOM kills)"
if ! swapon --show | grep -q '/swapfile'; then
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 4G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
  fi
  sudo swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf >/dev/null
sudo sysctl -p /etc/sysctl.d/99-swappiness.conf >/dev/null

echo "==> app directory $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER":"$USER" "$APP_DIR"

echo
echo "Done. LOG OUT and back in (or run: newgrp docker) so the docker group applies."
echo "Next: DEPLOY.md step 5 (GitHub deploy key)."
