#!/usr/bin/env bash
# Run explicitly as root on a dedicated Ubuntu 24.04 amd64 VPS.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ $EUID -eq 0 ]] || { echo 'Run with sudo on the target VPS' >&2; exit 1; }
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(dpkg --print-architecture) == amd64 ]] || {
  echo 'Requires Ubuntu 24.04 amd64' >&2; exit 1;
}
root=${1:-/srv/rag}
[[ $root == /srv/* && $root != *'/../'* && $root != */.. && $root != *$'\n'* ]] || {
  echo 'State root must be a canonical directory below /srv' >&2; exit 1;
}
[[ $(realpath -m "$root") == "$root" && ! -L $root ]] || exit 1
memory_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
[[ $memory_kib -ge 7602176 ]] || { echo 'Requires an 8 GB RAM VPS (swap excluded)' >&2; exit 1; }
[[ $(nproc) -ge 4 ]] || { echo 'Requires at least 4 vCPU' >&2; exit 1; }
install -d -o 10001 -g 10001 -m 750 "$root"
read -r total available < <(df -Pk "$root" | awk 'NR==2 {print $2, $4}')
[[ $total -ge 73400320 && $available -ge $((total / 5)) ]] || {
  echo 'Requires an 80 GB class disk and at least 20% free space' >&2; exit 1;
}
source deploy/docker-versions.env
if ! command -v docker >/dev/null; then
  for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
      echo "Conflicting package $package: resolve explicitly before retry" >&2; exit 1
    fi
  done
  apt-get update
  apt-get install -y ca-certificates curl python3
  install -d -m 755 /etc/apt/keyrings
  curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf '%s\n' 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable' > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y "docker-ce=$DOCKER_ENGINE_VERSION" "docker-ce-cli=$DOCKER_ENGINE_VERSION" \
    "containerd.io=$CONTAINERD_VERSION" "docker-buildx-plugin=$BUILDX_VERSION" "docker-compose-plugin=$COMPOSE_VERSION"
else
  # Never replace an existing host installation silently.
  for item in "docker-ce=$DOCKER_ENGINE_VERSION" "docker-ce-cli=$DOCKER_ENGINE_VERSION" \
      "containerd.io=$CONTAINERD_VERSION" "docker-buildx-plugin=$BUILDX_VERSION" "docker-compose-plugin=$COMPOSE_VERSION"; do
    [[ $(dpkg-query -W -f='${Version}' "${item%%=*}" 2>/dev/null) == "${item#*=}" ]] || {
      echo "Installed Docker packages differ from pinned $item; reconcile explicitly" >&2; exit 1;
    }
  done
fi
command -v python3 >/dev/null || { echo 'Install python3 before retry' >&2; exit 1; }
for directory in data faiss_indexes prepared_tables prepared_corpora model_cache policies logs bot secrets; do
  [[ ! -L $root/$directory ]] || { echo 'Linked state directory forbidden' >&2; exit 1; }
  install -d -o 10001 -g 10001 -m 700 "$root/$directory"
done
[[ ! -L $root/.model-owner.lock ]] || exit 1
touch "$root/.model-owner.lock"
chown 10001:10001 "$root/.model-owner.lock"
chmod 600 "$root/.model-owner.lock"
python3 -m scripts.init_secrets "$root/secrets"
for name in cli web tg health; do chown 10001:10001 "$root/secrets/$name.txt"; done
chown 10001:10001 "$root/secrets/adapters.json"
[[ ! -L $root/policies/external.json ]] || exit 1
if [[ ! -e $root/policies/external.json ]]; then
  install -o 10001 -g 10001 -m 600 deploy/privacy-policy.example.json "$root/policies/external.json"
fi
systemctl enable --now docker
docker compose version
printf 'State prepared: %s\nSet RAG_STATE_ROOT and RAG_SECRETS_ROOT in deploy/vps.env before starting Compose.\n' "$root"
