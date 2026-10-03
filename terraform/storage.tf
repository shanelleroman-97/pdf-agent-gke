# Uploaded PDFs. Written by the Submit API, read by the dispatcher. Sandboxes
# never touch GCS.
resource "google_storage_bucket" "inputs" {
  name                        = "${var.project_id}-pdf-agent-inputs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection

  lifecycle_rule {
    condition { age = var.input_retention_days }
    action { type = "Delete" }
  }

  depends_on = [google_project_service.required]
}

# Collected reports: <session_id>/{report.md,findings.json,outputs.tar.gz,status.json}.
# Written by the dispatcher, read by the Submit API.
resource "google_storage_bucket" "outputs" {
  name                        = "${var.project_id}-pdf-agent-outputs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection

  lifecycle_rule {
    condition { age = var.output_retention_days }
    action { type = "Delete" }
  }

  depends_on = [google_project_service.required]
}
