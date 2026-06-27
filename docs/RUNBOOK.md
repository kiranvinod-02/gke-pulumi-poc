# GKE Production POC — Runbook

## Architecture
- External HTTPS LB → Frontend + Backend services → GKE node pool (private, no public IPs)
- Internal HTTPS LB → ArgoCD / Grafana / Prometheus (VPC-only)
- Bastion VM (no external IP, IAP-only) + Tailscale — admin access
- GitHub Actions (WIF, no static keys) → builds images, updates Git manifests
- ArgoCD (App-of-Apps) → watches Git, deploys to cluster — CI never touches cluster directly

## Phase log

## Phase 0 — Manual bootstrap (one-time, can never be GitOps'd)

This is the only step done by hand, ever. Everything after this lives in Git and runs via GitHub Actions.

1. Created `github-actions-sa@kiran-499609.iam.gserviceaccount.com`
2. Granted roles: artifactregistry.admin, compute.admin, container.admin,
   iam.serviceAccountAdmin, iam.serviceAccountUser, iam.workloadIdentityPoolAdmin,
   resourcemanager.projectIamAdmin, storage.admin
3. Created WIF pool `github-pool` (location: global)
4. Created WIF provider `github-provider`, OIDC issuer = token.actions.githubusercontent.com,
   attribute_condition scoped to repo `kiranvinod-02/gke-pulumi-poc`
5. Bound the SA to the WIF principalSet via `roles/iam.workloadIdentityUser`

Gotcha: WIF pool/provider IDs are soft-deleted for 30 days after destroy — reusing the
same ID requires `gcloud iam workload-identity-pools undelete` /
`providers undelete`, not `create`.
