# One Google service account per component, each with only what it uses.

# GKE nodes: logging/monitoring and image pulls, nothing else.
resource "google_service_account" "nodes" {
  account_id   = "pdf-agent-nodes"
  display_name = "PDF agent - GKE nodes"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset(["roles/container.defaultNodeServiceAccount"])
  project  = var.project_id
  role     = each.key
  member   = google_service_account.nodes.member
}

resource "google_artifact_registry_repository_iam_member" "nodes_pull" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = google_service_account.nodes.member
}

# Dispatcher (GKE, Workload Identity): read inputs, write outputs.
resource "google_service_account" "dispatcher" {
  account_id   = "pdf-agent-dispatcher"
  display_name = "PDF agent - dispatcher"
}

resource "google_storage_bucket_iam_member" "dispatcher_inputs" {
  bucket = google_storage_bucket.inputs.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.dispatcher.member
}

resource "google_storage_bucket_iam_member" "dispatcher_outputs" {
  bucket = google_storage_bucket.outputs.name
  role   = "roles/storage.objectUser"
  member = google_service_account.dispatcher.member
}

# Stats adapter (GKE, Workload Identity): write the queue-depth metric.
resource "google_service_account" "stats_adapter" {
  account_id   = "pdf-agent-stats-adapter"
  display_name = "PDF agent - stats adapter"
}

resource "google_project_iam_member" "stats_adapter_metrics" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = google_service_account.stats_adapter.member
}

resource "google_service_account_iam_member" "workload_identity" {
  for_each = {
    "pdf-agent-dispatcher"    = google_service_account.dispatcher.name
    "pdf-agent-stats-adapter" = google_service_account.stats_adapter.name
  }
  service_account_id = each.value
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.k8s_namespace}/${each.key}]"
  depends_on         = [google_container_cluster.this]
}

# Submit API (Cloud Run): write inputs, read outputs, read the API key.
resource "google_service_account" "submit_api" {
  account_id   = "pdf-agent-submit-api"
  display_name = "PDF agent - Submit API"
}

resource "google_storage_bucket_iam_member" "submit_api_inputs" {
  bucket = google_storage_bucket.inputs.name
  role   = "roles/storage.objectUser"
  member = google_service_account.submit_api.member
}

resource "google_storage_bucket_iam_member" "submit_api_outputs" {
  bucket = google_storage_bucket.outputs.name
  role   = "roles/storage.objectViewer"
  member = google_service_account.submit_api.member
}

resource "google_secret_manager_secret_iam_member" "submit_api_key" {
  secret_id = google_secret_manager_secret.api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.submit_api.member
}

# Cloud Build: push images to this one repository.
resource "google_service_account" "build" {
  account_id   = "pdf-agent-build"
  display_name = "PDF agent - Cloud Build"
}

resource "google_artifact_registry_repository_iam_member" "build_push" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.build.member
}

resource "google_project_iam_member" "build" {
  for_each = toset(["roles/logging.logWriter", "roles/storage.objectViewer"])
  project  = var.project_id
  role     = each.key
  member   = google_service_account.build.member
}
