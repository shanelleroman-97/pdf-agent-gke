output "cluster_name" {
  value = google_container_cluster.this.name
}

output "artifact_registry" {
  description = "Docker repo path prefix for built images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "inputs_bucket" {
  value = google_storage_bucket.inputs.name
}

output "outputs_bucket" {
  value = google_storage_bucket.outputs.name
}

output "dispatcher_gsa" {
  value = google_service_account.dispatcher.email
}

output "stats_adapter_gsa" {
  value = google_service_account.stats_adapter.email
}

output "submit_api_gsa" {
  value = google_service_account.submit_api.email
}

output "build_gsa" {
  value = google_service_account.build.email
}
