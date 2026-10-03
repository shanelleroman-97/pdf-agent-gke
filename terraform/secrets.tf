# Secrets are declared here but their values are added out-of-band with
# `make secrets`, so keys never appear in Terraform variables or state.
resource "google_secret_manager_secret" "env_key" {
  secret_id = "pdf-agent-anthropic-environment-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "api_key" {
  secret_id = "pdf-agent-anthropic-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}
