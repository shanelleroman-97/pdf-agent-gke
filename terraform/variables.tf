variable "project_id" {
  type        = string
  description = "Google Cloud project ID."
}

variable "region" {
  type        = string
  description = "Region for the cluster, buckets, registry, and Cloud Run."
  default     = "us-central1"
}

variable "cluster_name" {
  type    = string
  default = "pdf-agent"
}

variable "gke_min_version" {
  type        = string
  description = <<-EOT
    Optional minimum GKE control-plane version. Null uses the RAPID channel's
    default, which tracks what the Agent Sandbox add-on needs (>= 1.35.2).
    Pinning a specific patch breaks once GKE retires it.
  EOT
  default     = null
}

variable "k8s_namespace" {
  type    = string
  default = "pdf-agent"
}

variable "master_authorized_cidrs" {
  type        = list(string)
  description = "CIDRs allowed to reach the GKE control plane. Empty means any (still requires IAM)."
  default     = []
}

variable "input_retention_days" {
  type        = number
  description = "Delete uploaded PDFs after this many days."
  default     = 30
}

variable "output_retention_days" {
  type        = number
  description = "Delete collected reports after this many days."
  default     = 90
}

variable "deletion_protection" {
  type        = bool
  description = "Block `terraform destroy` of the cluster and non-empty buckets."
  default     = true
}
