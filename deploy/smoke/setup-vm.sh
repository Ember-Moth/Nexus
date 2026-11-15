#!/usr/bin/env bash
# Prepare a disposable Ubuntu VM (deploy/smoke/lima-k3s.yaml) as a single-node k3s "production"
# host for the smoke test: the same k3s flags as docs/deployment.md, plus local stand-ins for
# the things a real deployment buys (S3 storage, ACME). Idempotent; run inside the VM from the
# repository root:
#   bash deploy/smoke/setup-vm.sh
set -euo pipefail

K3S_VERSION=${K3S_VERSION:-v1.36.6+k3s1}
HELM_VERSION=${HELM_VERSION:-v4.3.2}
CERT_MANAGER_VERSION=${CERT_MANAGER_VERSION:-v1.21.3}
CNPG_VERSION=${CNPG_VERSION:-1.30.1}
BARMAN_PLUGIN_VERSION=${BARMAN_PLUGIN_VERSION:-v0.16.0}
VERSITYGW_IMAGE=${VERSITYGW_IMAGE:-versity/versitygw:v1.11.0}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
STATE=${SMOKE_STATE:-$HOME/nexus-smoke}
NODE_IP=${NODE_IP:-$(ip -4 -o addr show lima0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)}
NODE_IP=${NODE_IP:-$(hostname -I | awk '{print $1}')}
ARCH=$(dpkg --print-architecture)
mkdir -p "$STATE" && chmod 700 "$STATE"

log() { printf '\n== %s\n' "$*"; }

# --- S3-compatible stand-in (outside the cluster it backs up) ----------------------------------
log "S3 stand-in (versitygw) on $NODE_IP:7070"
if [ ! -f "$STATE/s3.env" ]; then
  umask 077
  printf 'S3_ACCESS_KEY=%s\nS3_SECRET_KEY=%s\n' "$(openssl rand -hex 10)" "$(openssl rand -hex 20)" > "$STATE/s3.env"
fi
# shellcheck disable=SC1091
. "$STATE/s3.env"
mkdir -p "$STATE/s3data"
if ! nerdctl ps --format '{{.Names}}' | grep -qx nexus-s3; then
  nerdctl rm -f nexus-s3 >/dev/null 2>&1 || true
  nerdctl run -d --name nexus-s3 --restart always -p 7070:7070 \
    -v "$STATE/s3data:/data" "$VERSITYGW_IMAGE" \
    --access "$S3_ACCESS_KEY" --secret "$S3_SECRET_KEY" posix /data >/dev/null
fi
# --noproxy: Lima passes the host's proxy settings into the VM.
for _ in $(seq 1 30); do curl --noproxy '*' -s -o /dev/null "http://$NODE_IP:7070/" && break; sleep 1; done
for bucket in nexus-etcd-snapshots nexus-pg-backups; do
  status=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --aws-sigv4 "aws:amz:us-east-1:s3" \
    --user "$S3_ACCESS_KEY:$S3_SECRET_KEY" -X PUT "http://$NODE_IP:7070/$bucket")
  case $status in 200|409) ;; *) echo "creating bucket $bucket failed: HTTP $status" >&2; exit 1 ;; esac
done

# --- k3s ----------------------------------------------------------------------------------------
log "k3s $K3S_VERSION (embedded etcd, secrets encryption, etcd snapshots to S3)"
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<EOF
cluster-init: true
secrets-encryption: true
write-kubeconfig-mode: "0600"
node-ip: $NODE_IP
flannel-iface: lima0
tls-san:
  - $NODE_IP
etcd-snapshot-schedule-cron: "0 */6 * * *"
etcd-snapshot-retention: 28
etcd-snapshot-compress: true
etcd-s3: true
etcd-s3-endpoint: $NODE_IP:7070
etcd-s3-insecure: true
etcd-s3-region: us-east-1
etcd-s3-bucket: nexus-etcd-snapshots
etcd-s3-folder: smoke
etcd-s3-access-key: $S3_ACCESS_KEY
etcd-s3-secret-key: $S3_SECRET_KEY
kubelet-arg:
  - "system-reserved=cpu=250m,memory=512Mi"
  - "eviction-hard=memory.available<300Mi,nodefs.available<10%"
EOF
sudo chmod 600 /etc/rancher/k3s/config.yaml
if ! command -v k3s >/dev/null || [ "$(k3s --version | head -1 | awk '{print $3}')" != "$K3S_VERSION" ]; then
  curl -sfL https://get.k3s.io | sudo INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server
fi
# The installer copies the host's proxy variables into k3s's environment. Keep the node's own
# address off the proxy, or the API server's requests to the kubelet (logs) go through it.
if sudo grep -qi '^https\?_proxy=' /etc/systemd/system/k3s.service.env 2>/dev/null \
  && ! sudo grep -q "$NODE_IP" /etc/systemd/system/k3s.service.env; then
  sudo sed -i -E "s#^(NO_PROXY|no_proxy)='?([^']*)'?#\1='\2,$NODE_IP,.svc,.cluster.local'#" \
    /etc/systemd/system/k3s.service.env
  sudo systemctl restart k3s
fi
mkdir -p "$HOME/.kube"
sudo cat /etc/rancher/k3s/k3s.yaml > "$HOME/.kube/config" && chmod 600 "$HOME/.kube/config"
export KUBECONFIG=$HOME/.kube/config
# Non-interactive shells (limactl shell, ssh) get it too.
grep -q '^KUBECONFIG=' /etc/environment || echo "KUBECONFIG=$HOME/.kube/config" | sudo tee -a /etc/environment >/dev/null
command -v kubectl >/dev/null || sudo ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl
until kubectl get nodes >/dev/null 2>&1; do sleep 2; done
kubectl wait --for=condition=Ready node --all --timeout=180s

# --- Helm ---------------------------------------------------------------------------------------
if ! helm version --short 2>/dev/null | grep -q "^${HELM_VERSION}"; then
  log "helm $HELM_VERSION"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/helm.tgz" "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz"
  curl -fsSL -o "$tmp/helm.tgz.sha256sum" "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz.sha256sum"
  (cd "$tmp" && sed "s#helm-${HELM_VERSION}-linux-${ARCH}.tar.gz#helm.tgz#" helm.tgz.sha256sum | sha256sum -c -)
  tar -C "$tmp" -xzf "$tmp/helm.tgz" && sudo install -m 0755 "$tmp/linux-${ARCH}/helm" /usr/local/bin/helm
  rm -rf "$tmp"
fi

# --- Cluster add-ons ------------------------------------------------------------------------------
log "Traefik settings (externalTrafficPolicy Local, SSE/WS timeouts)"
kubectl apply -f "$REPO/deploy/k3s/traefik-config.yaml"

log "cert-manager $CERT_MANAGER_VERSION"
kubectl apply --server-side -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml" >/dev/null
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s

log "CloudNativePG $CNPG_VERSION and Barman Cloud plugin $BARMAN_PLUGIN_VERSION"
kubectl apply --server-side -f "https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-${CNPG_VERSION%.*}/releases/cnpg-${CNPG_VERSION}.yaml" >/dev/null
kubectl -n cnpg-system rollout status deploy/cnpg-controller-manager --timeout=300s
kubectl apply -f "https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/${BARMAN_PLUGIN_VERSION}/manifest.yaml" >/dev/null
kubectl -n cnpg-system rollout status deploy/barman-cloud --timeout=300s

# Stand-in for the ACME issuer: a private CA. The smoke client trusts its root.
log "smoke ClusterIssuer (private CA instead of ACME)"
kubectl apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: {name: smoke-selfsigned}
spec: {selfSigned: {}}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {name: smoke-root-ca, namespace: cert-manager}
spec:
  isCA: true
  commonName: Nexus smoke root CA
  secretName: smoke-root-ca
  duration: 87600h
  privateKey: {algorithm: ECDSA, size: 256}
  issuerRef: {name: smoke-selfsigned, kind: ClusterIssuer}
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata: {name: smoke-ca}
spec: {ca: {secretName: smoke-root-ca}}
EOF
kubectl -n cert-manager wait --for=condition=Ready certificate/smoke-root-ca --timeout=120s

# The namespace and the backup credentials (this script owns the S3 stand-in's keys).
kubectl create namespace nexus --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n nexus create secret generic nexus-backup-s3 \
  --from-env-file=<(printf 'ACCESS_KEY_ID=%s\nACCESS_SECRET_KEY=%s\n' "$S3_ACCESS_KEY" "$S3_SECRET_KEY") \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# --- Images --------------------------------------------------------------------------------------
log "import images into k3s containerd"
for image in control-plane relay web; do
  nerdctl save "nexus/$image:smoke" | sudo k3s ctr -n k8s.io images import - >/dev/null
done
sudo k3s ctr -n k8s.io images ls -q | grep 'nexus/' || true
log "ready: kubectl get nodes -o wide"
kubectl get nodes -o wide
