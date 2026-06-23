import pulumi
import pulumi_gcp as gcp

config = pulumi.Config()
project = gcp.config.project
region = gcp.config.region or "asia-south1"
zone = f"{region}-b"

# VPC
vpc = gcp.compute.Network("poc-vpc",
    auto_create_subnetworks=False,
)

subnet = gcp.compute.Subnetwork("poc-subnet",
    network=vpc.id,
    ip_cidr_range="10.10.0.0/20",
    region=region,
    secondary_ip_ranges=[
        gcp.compute.SubnetworkSecondaryIpRangeArgs(
            range_name="pods",
            ip_cidr_range="10.20.0.0/16",
        ),
        gcp.compute.SubnetworkSecondaryIpRangeArgs(
            range_name="services",
            ip_cidr_range="10.30.0.0/20",
        ),
    ],
    private_ip_google_access=True,
)

# Cloud Router + Cloud NAT — outbound internet for the bastion (no public IP)
router = gcp.compute.Router("poc-router",
    network=vpc.id,
    region=region,
)

nat = gcp.compute.RouterNat("poc-nat",
    router=router.name,
    region=region,
    nat_ip_allocate_option="AUTO_ONLY",
    source_subnetwork_ip_ranges_to_nat="ALL_SUBNETWORKS_ALL_IP_RANGES",
)

# Firewall: allow IAP's fixed source range to reach the bastion on port 22
iap_firewall = gcp.compute.Firewall("allow-iap-ssh",
    network=vpc.id,
    direction="INGRESS",
    source_ranges=["35.235.240.0/20"],
    allows=[gcp.compute.FirewallAllowArgs(
        protocol="tcp",
        ports=["22"],
    )],
    target_tags=["bastion"],
)

# Bastion VM — no external IP, reachable only via IAP
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
    )],
    metadata={
        "enable-oslogin": "TRUE",
    },
    service_account=gcp.compute.InstanceServiceAccountArgs(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    ),
)

# Artifact Registry
artifact_repo = gcp.artifactregistry.Repository("poc-repo",
    location=region,
    repository_id="poc-repo",
    format="DOCKER",
)

# GKE cluster (public for now — private config comes once IAM permissions land)
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
    deletion_protection=False,
)

node_pool = gcp.container.NodePool("poc-node-pool",
    cluster=cluster.name,
    location=zone,
    node_count=2,
    node_config=gcp.container.NodePoolNodeConfigArgs(
        machine_type="e2-small",
        oauth_scopes=[
            "https://www.googleapis.com/auth/cloud-platform",
        ],
    ),
)

# ---------------------------------------------------------------------------
# GitHub Actions Service Account — identity CI jobs impersonate via WIF.
# Scoped to exactly: push images, manage Pulumi state. No cluster access —
# ArgoCD is the only thing that ever applies changes to GKE.
# ---------------------------------------------------------------------------
github_actions_sa = gcp.serviceaccount.Account("github-actions-sa",
    account_id="github-actions-sa",
    display_name="GitHub Actions CI Service Account",
    description="Used by GitHub Actions via WIF to push images and run pulumi up",
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

pulumi.export("cluster_name", cluster.name)
pulumi.export("artifact_registry", artifact_repo.name)
pulumi.export("bastion_name", bastion.name)
pulumi.export("ssh_to_bastion_cmd", pulumi.Output.concat(
    "gcloud compute ssh ", bastion.name,
    " --zone ", zone, " --project ", project, " --tunnel-through-iap"
))
pulumi.export("kubeconfig_cmd", pulumi.Output.concat(
    "gcloud container clusters get-credentials ", cluster.name,
    " --zone ", zone, " --project ", project
))
pulumi.export("github_actions_sa_email", github_actions_sa.email)
