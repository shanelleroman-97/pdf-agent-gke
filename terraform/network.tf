# Dedicated VPC with private nodes. Nodes have no public IPs; outbound
# traffic (api.anthropic.com, image pulls) leaves through Cloud NAT.
resource "google_compute_network" "this" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "nodes" {
  name                     = "${var.cluster_name}-nodes"
  network                  = google_compute_network.this.id
  region                   = var.region
  ip_cidr_range            = "10.10.0.0/20"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/14"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.30.0.0/20"
  }
}

resource "google_compute_router" "this" {
  name    = "${var.cluster_name}-router"
  network = google_compute_network.this.id
  region  = var.region
}

resource "google_compute_router_nat" "this" {
  name                               = "${var.cluster_name}-nat"
  router                             = google_compute_router.this.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
