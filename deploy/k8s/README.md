# K3s deployment

This directory contains the Kubernetes manifests for running the dashboard on
the single-node K3s host. It deliberately uses static hostPath PVs under
`/Data/k3s-storage` instead of changing K3s' cluster-wide `local-path`
StorageClass. That keeps existing LiteLLM PVCs on their current path.

## Preparation

1. Create `/Data/k3s-storage/{postgres,redis,app-data,mail-dkim,mail-spool}` on
   the node and set ownership/permissions for the containers.
2. Import the exact `ai-token-dashboard:latest` image into K3s containerd:
   `docker save ai-token-dashboard:latest | ssh ... 'sudo k3s ctr images import -'`.
3. Generate the Secret from a protected, untracked production `.env`, for
   example `kubectl create secret generic dashboard-secrets --from-env-file=.env`.
   Never commit the resulting Secret or a production `.env`. The checked-in
   `secret.example.yaml` is a reference only and is intentionally not included
   in `kustomization.yaml`.
4. Apply `kustomization.yaml`, then restore PostgreSQL and the persistent app
   data before starting the workers.
5. Apply `cloudflared.yaml` only after replacing its tunnel UUID and creating
   the credential Secret; it is intentionally excluded from the base bundle.

The manifests do not stop the old Compose deployment or switch Cloudflare.
Perform those actions only during the approved maintenance window described in
the migration runbook.
