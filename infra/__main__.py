import pulumi
import pulumi_gcp as gcp

config = pulumi.Config()
project = gcp.config.project
region = gcp.config.region or "asia-south1"
zone = f"{region}-b"

# ---------------------------------------------------------------------------
# VPC — custom mode, three-tier IP ranges for GKE VPC-native networking
# ---------------------------------------------------------------------------
vpc = gcp.compute.Network("poc-vpc", auto_create_subnetworks=False)

subnet = gcp.compute.Subnetwork("poc-subnet",
    network=vpc.id,
    ip_cidr_range="10.10.0.0/20",        # nodes
    region=region,
    secondary_ip_ranges=[
        gcp.compute.SubnetworkSecondaryIpRangeArgs(range_name="pods",     ip_cidr_range="10.20.0.0/16"),
        gcp.compute.SubnetworkSecondaryIpRangeArgs(range_name="services", ip_cidr_range="10.30.0.0/20"),
    ],
    private_ip_google_access=True,
)

# ---------------------------------------------------------------------------
# Cloud Router + NAT — outbound internet for private nodes and bastion
# ---------------------------------------------------------------------------
router = gcp.compute.Router("poc-router", network=vpc.id, region=region)

nat = gcp.compute.RouterNat("poc-nat",
    router=router.name,
    region=region,
    nat_ip_allocate_option="AUTO_ONLY",
    source_subnetwork_ip_ranges_to_nat="ALL_SUBNETWORKS_ALL_IP_RANGES",
)

# ---------------------------------------------------------------------------
# Static IPs
# Global IP  → External HTTPS LB (internet-facing, user traffic)
# Regional IP → Internal LB (VPC-only, admin tools: ArgoCD, Grafana)
# ---------------------------------------------------------------------------
external_ip = gcp.compute.GlobalAddress("nginx-external-ip",
    name="nginx-external-ip",
    description="Static IP for External HTTPS LB — user/app traffic",
)

internal_ip = gcp.compute.Address("argocd-internal-ip",
    name="argocd-internal-ip",
    region=region,
    address_type="INTERNAL",
    subnetwork=subnet.id,
    description="Static internal IP for Internal LB — ArgoCD/Grafana admin access",
)
pulumi.export("external_ip", external_ip.address)
pulumi.export("internal_ip", internal_ip.address)

# ---------------------------------------------------------------------------
# Cloud Armor — WAF policy applied to External LB
# Matches the architecture: Cloud Armor sits between internet and backend services
# ---------------------------------------------------------------------------
armor_policy = gcp.compute.SecurityPolicy("cloud-armor-policy",
    description="WAF policy for nginx external LB — matches Parallel Loop target architecture",
    rules=[
        gcp.compute.SecurityPolicyRuleArgs(
            action="deny(403)",
            priority=1000,
            match=gcp.compute.SecurityPolicyRuleMatchArgs(
                expr=gcp.compute.SecurityPolicyRuleMatchExprArgs(
                    expression="evaluatePreconfiguredExpr('xss-stable')",
                ),
            ),
            description="Block XSS attacks",
        ),
        gcp.compute.SecurityPolicyRuleArgs(
            action="deny(403)",
            priority=1001,
            match=gcp.compute.SecurityPolicyRuleMatchArgs(
                expr=gcp.compute.SecurityPolicyRuleMatchExprArgs(
                    expression="evaluatePreconfiguredExpr('sqli-stable')",
                ),
            ),
            description="Block SQLi attacks",
        ),
        gcp.compute.SecurityPolicyRuleArgs(
            action="allow",
            priority=2147483647,
            match=gcp.compute.SecurityPolicyRuleMatchArgs(
                versioned_expr="SRC_IPS_V1",
                config=gcp.compute.SecurityPolicyRuleMatchConfigArgs(
                    src_ip_ranges=["*"],
                ),
            ),
            description="Default allow rule",
        ),
    ],
)

# ---------------------------------------------------------------------------
# Firewall rules
# ---------------------------------------------------------------------------

# IAP → bastion (port 22) — 35.235.240.0/20 is Google's fixed IAP range
iap_firewall = gcp.compute.Firewall("allow-iap-ssh",
    network=vpc.id,
    direction="INGRESS",
    source_ranges=["35.235.240.0/20"],
    allows=[gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["22"])],
    target_tags=["bastion"],
)

# GKE health checks — GCP LB probes come from these ranges
lb_health_check_firewall = gcp.compute.Firewall("allow-lb-health-checks",
    network=vpc.id,
    direction="INGRESS",
    source_ranges=["130.211.0.0/22", "35.191.0.0/16"],
    allows=[gcp.compute.FirewallAllowArgs(protocol="tcp", ports=["80", "8080", "443"])],
    target_tags=["gke-node"],
)

# ---------------------------------------------------------------------------
# Bastion VM — no external IP, reachable only via IAP
# This is the ONLY admin path into the private cluster
# ---------------------------------------------------------------------------
bastion = gcp.compute.Instance("poc-bastion",
    machine_type="e2-small",
    zone=zone,
    tags=["bastion"],
    boot_disk=gcp.compute.InstanceBootDiskArgs(
        initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
            image="debian-cloud/debian-12",
        ),
    ),
    network_interfaces=[gcp.compute.InstanceNetworkInterfaceArgs(
        network=vpc.id,
        subnetwork=subnet.id,
        # No access_config = no external IP
    )],
    metadata={"enable-oslogin": "TRUE"},
    service_account=gcp.compute.InstanceServiceAccountArgs(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    ),
)

# ---------------------------------------------------------------------------
# Artifact Registry — container image storage (replaces ECR)
# ---------------------------------------------------------------------------
artifact_repo = gcp.artifactregistry.Repository("poc-repo",
    location=region,
    repository_id="poc-repo",
    format="DOCKER",
)

# ---------------------------------------------------------------------------
# GKE Cluster — FULLY PRIVATE
# enable_private_nodes=True     → worker nodes get no public IPs
# enable_private_endpoint=True  → control plane only reachable from VPC
# master_authorized_networks    → only bastion subnet can reach control plane
# master_ipv4_cidr_block        → /28 reserved for control plane internals
# ---------------------------------------------------------------------------
cluster = gcp.container.Cluster("poc-cluster",
    location=zone,
    network=vpc.id,
    subnetwork=subnet.id,
    remove_default_node_pool=True,
    initial_node_count=1,
    ip_allocation_policy=gcp.container.ClusterIpAllocationPolicyArgs(
        cluster_secondary_range_name="pods",
        services_secondary_range_name="services",
    ),
    private_cluster_config=gcp.container.ClusterPrivateClusterConfigArgs(
        enable_private_nodes=True,
        enable_private_endpoint=True,
        master_ipv4_cidr_block="172.16.0.0/28",
    ),
    master_authorized_networks_config=gcp.container.ClusterMasterAuthorizedNetworksConfigArgs(
        cidr_blocks=[
            gcp.container.ClusterMasterAuthorizedNetworksConfigCidrBlockArgs(
                cidr_block="10.10.0.0/20",
                display_name="bastion-subnet",
            ),
        ],
    ),
    # Workload Identity — lets pods assume GCP SA roles without static keys
    workload_identity_config=gcp.container.ClusterWorkloadIdentityConfigArgs(
        workload_pool=f"{project}.svc.id.goog",
    ),
    addons_config=gcp.container.ClusterAddonsConfigArgs(
        # HTTP load balancing addon — required for GKE Ingress to provision GCP LBs
        http_load_balancing=gcp.container.ClusterAddonsConfigHttpLoadBalancingArgs(
            disabled=False,
        ),
    ),
    deletion_protection=False,
)

# Node pool — separate from cluster for independent lifecycle management
node_pool = gcp.container.NodePool("poc-node-pool",
    cluster=cluster.name,
    location=zone,
    node_count=2,
    node_config=gcp.container.NodePoolNodeConfigArgs(
        machine_type="e2-standard-2",  # 2 vCPU, 8GB — handles Prometheus+Grafana+ArgoCD+nginx
        oauth_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        # Tag nodes so LB health check firewall rule applies
        tags=["gke-node"],
        workload_metadata_config=gcp.container.NodePoolNodeConfigWorkloadMetadataConfigArgs(
            mode="GKE_METADATA",  # enables Workload Identity on nodes
        ),
    ),
    autoscaling=gcp.container.NodePoolAutoscalingArgs(
        min_node_count=2,
        max_node_count=5,
    ),
)

# ---------------------------------------------------------------------------
# GitHub Actions Service Account — CI identity, zero cluster access
# ArgoCD is the ONLY thing that deploys to the cluster
# ---------------------------------------------------------------------------
github_actions_sa = gcp.serviceaccount.Account("github-actions-sa",
    account_id="github-actions-sa",
    display_name="GitHub Actions CI Service Account",
    description="Used by GitHub Actions via WIF — push images + run pulumi up only",
)

github_sa_artifact_writer = gcp.projects.IAMMember("github-sa-artifact-writer",
    project=project,
    role="roles/artifactregistry.writer",
    member=pulumi.Output.concat("serviceAccount:", github_actions_sa.email),
)

github_sa_storage_admin = gcp.projects.IAMMember("github-sa-storage-admin",
    project=project,
    role="roles/storage.objectAdmin",
    member=pulumi.Output.concat("serviceAccount:", github_actions_sa.email),
)

# Node pool SA needs to pull images from Artifact Registry
project_number = gcp.organizations.get_project(project_id=project).number
compute_sa_registry = gcp.projects.IAMMember("compute-sa-artifact-reader",
    project=project,
    role="roles/artifactregistry.reader",
    member=f"serviceAccount:{project_number}-compute@developer.gserviceaccount.com",
)

# ---------------------------------------------------------------------------
# WIF — GitHub Actions auth to GCP without static keys
# pool → provider → SA binding = three-piece trust chain
# ---------------------------------------------------------------------------
wif_pool = gcp.iam.WorkloadIdentityPool("github-wif-pool",
    workload_identity_pool_id="github-pool",
    display_name="GitHub Actions Pool",
    description="WIF pool for GitHub Actions CI",
)

wif_provider = gcp.iam.WorkloadIdentityPoolProvider("github-wif-provider",
    workload_identity_pool_id=wif_pool.workload_identity_pool_id,
    workload_identity_pool_provider_id="github-provider",
    display_name="GitHub Provider",
    oidc=gcp.iam.WorkloadIdentityPoolProviderOidcArgs(
        issuer_uri="https://token.actions.githubusercontent.com",
    ),
    attribute_mapping={
        "google.subject":       "assertion.sub",
        "attribute.actor":      "assertion.actor",
        "attribute.repository": "assertion.repository",
    },
    attribute_condition="assertion.repository == 'kiranvinod-02/gke-pulumi-poc'",
)

wif_sa_binding = gcp.serviceaccount.IAMMember("github-wif-sa-binding",
    service_account_id=github_actions_sa.name,
    role="roles/iam.workloadIdentityUser",
    member=pulumi.Output.concat(
        "principalSet://iam.googleapis.com/",
        wif_pool.name,
        "/attribute.repository/kiranvinod-02/gke-pulumi-poc",
    ),
)

# ---------------------------------------------------------------------------
# Outputs — everything needed for post-apply steps
# ---------------------------------------------------------------------------
pulumi.export("cluster_name", cluster.name)
pulumi.export("artifact_registry", artifact_repo.name)
pulumi.export("bastion_name", bastion.name)
pulumi.export("external_ip", external_ip.address)
pulumi.export("internal_ip", internal_ip.address)
pulumi.export("github_actions_sa_email", github_actions_sa.email)
pulumi.export("ssh_to_bastion_cmd", pulumi.Output.concat(
    "gcloud compute ssh ", bastion.name,
    " --zone ", zone, " --project ", project, " --tunnel-through-iap"
))
pulumi.export("kubeconfig_cmd_from_bastion", pulumi.Output.concat(
    "gcloud container clusters get-credentials ", cluster.name,
    " --zone ", zone, " --project ", project, " --internal-ip"
))
pulumi.export("wif_provider_name", wif_provider.name)
