resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "iam.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.key
  disable_on_destroy = false
}

resource "google_container_cluster" "this" {
  provider = google-beta

  name                = var.cluster_name
  location            = var.region
  enable_autopilot    = true
  deletion_protection = var.deletion_protection

  network    = google_compute_network.this.id
  subnetwork = google_compute_subnetwork.nodes.id
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.master_authorized_cidrs) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.master_authorized_cidrs
        content {
          cidr_block = cidr_blocks.value
        }
      }
    }
  }

  # Agent Sandbox needs a recent control plane; RAPID tracks it.
  min_master_version = var.gke_min_version
  release_channel {
    channel = "RAPID"
  }

  enable_fqdn_network_policy = true

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = google_service_account.nodes.email
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    }
  }

  depends_on = [google_project_service.required, google_project_iam_member.nodes]
}

# The add-on has no Terraform field yet. Most of the ~15 minute apply is this.
resource "null_resource" "enable_agent_sandbox" {
  triggers = {
    cluster = google_container_cluster.this.id
  }
  provisioner "local-exec" {
    command = <<-EOT
      gcloud beta container clusters update ${google_container_cluster.this.name} \
        --enable-agent-sandbox \
        --region ${var.region} --project ${var.project_id}
    EOT
  }
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "pdf-agent"
  location      = var.region
  format        = "DOCKER"
  depends_on    = [google_project_service.required]

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }
}
