# Remote state. `make infra` passes -backend-config="bucket=<project>-pdf-agent-tfstate"
# (created by `make bootstrap`), so state is shared and never on a laptop.
terraform {
  backend "gcs" {
    prefix = "pdf-agent"
  }
}
