# Run `source .env` first. Typical first run:
#   make bootstrap infra secrets images deploy api
PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
REGION     ?= us-central1
CLUSTER    ?= pdf-agent
NAMESPACE  := pdf-agent
OVERLAY    ?= 01_single_session
REGISTRY   := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/pdf-agent
TAG        ?= $(shell date +%Y%m%d%H%M%S)
# Expand once: a recursively expanded $(shell date) changes between uses.
TAG        := $(TAG)
STATE_BUCKET := $(PROJECT_ID)-pdf-agent-tfstate
INPUTS_BUCKET := $(PROJECT_ID)-pdf-agent-inputs
OUTPUTS_BUCKET := $(PROJECT_ID)-pdf-agent-outputs
GSA = pdf-agent-$(1)@$(PROJECT_ID).iam.gserviceaccount.com
SECRET_ENV_KEY := pdf-agent-anthropic-environment-key
SECRET_API_KEY := pdf-agent-anthropic-api-key

.PHONY: check bootstrap infra secrets images creds k8s-secrets deploy api agent status verify-egress pause resume destroy

check:
	@test -n "$(PROJECT_ID)" || { echo "PROJECT_ID is empty: source .env"; exit 1; }
	@test -n "$(TF_VAR_project_id)" || { echo "TF_VAR_project_id is empty: source .env"; exit 1; }

## One-time: versioned bucket for Terraform state.
bootstrap: check
	gcloud services enable storage.googleapis.com --project $(PROJECT_ID)
	gcloud storage buckets describe gs://$(STATE_BUCKET) --project $(PROJECT_ID) >/dev/null 2>&1 || \
	  gcloud storage buckets create gs://$(STATE_BUCKET) --project $(PROJECT_ID) --location $(REGION) \
	    --uniform-bucket-level-access --public-access-prevention
	gcloud storage buckets update gs://$(STATE_BUCKET) --versioning

## Cluster, network, buckets, registry, secrets (empty), service accounts. ~20 min.
infra: check
	cd terraform && terraform init -input=false -backend-config="bucket=$(STATE_BUCKET)" && terraform apply

## Add secret values from your shell env (never through Terraform).
secrets: check
	@test -n "$$ANTHROPIC_API_KEY" -a -n "$$ANTHROPIC_ENVIRONMENT_KEY" || { echo "set both keys in .env"; exit 1; }
	@printf '%s' "$$ANTHROPIC_API_KEY" | gcloud secrets versions add $(SECRET_API_KEY) --data-file=- --project $(PROJECT_ID)
	@printf '%s' "$$ANTHROPIC_ENVIRONMENT_KEY" | gcloud secrets versions add $(SECRET_ENV_KEY) --data-file=- --project $(PROJECT_ID)

images: check
	gcloud builds submit . --config cloudbuild.yaml \
	  --substitutions _TAG=$(TAG),_REGISTRY=$(REGISTRY) \
	  --service-account projects/$(PROJECT_ID)/serviceAccounts/$(call GSA,build) \
	  --default-buckets-behavior regional-user-owned-bucket \
	  --region $(REGION) --project $(PROJECT_ID)
	@echo "$(TAG)" > .last-image-tag

creds: check
	gcloud container clusters get-credentials $(CLUSTER) --region $(REGION) --project $(PROJECT_ID)

## Mirror Secret Manager values into Kubernetes Secrets for the pods.
k8s-secrets: creds
	kubectl get namespace $(NAMESPACE) >/dev/null 2>&1 || kubectl create namespace $(NAMESPACE)
	@for pair in anthropic-environment-key:$(SECRET_ENV_KEY) anthropic-api-key:$(SECRET_API_KEY); do \
	  name=$${pair%%:*}; secret=$${pair#*:}; \
	  v=$$(gcloud secrets versions access latest --secret $$secret --project $(PROJECT_ID)) || exit 1; \
	  kubectl -n $(NAMESPACE) create secret generic $$name --from-literal=value="$$v" \
	    --dry-run=client -o yaml | kubectl apply -f - ; \
	done

deploy: k8s-secrets
	@test -n "$$ANTHROPIC_ENVIRONMENT_ID" || { echo "ANTHROPIC_ENVIRONMENT_ID is empty: source .env"; exit 1; }
	$(eval TAG := $(shell cat .last-image-tag 2>/dev/null || echo $(TAG)))
	cd deploy/overlays/$(OVERLAY) && \
	printf 'environment_id=%s\ninputs_bucket=%s\noutputs_bucket=%s\nproject_id=%s\n' \
	  "$$ANTHROPIC_ENVIRONMENT_ID" "$(INPUTS_BUCKET)" "$(OUTPUTS_BUCKET)" "$(PROJECT_ID)" > params.env && \
	printf '%s\n' \
	  'apiVersion: v1' 'kind: ServiceAccount' 'metadata:' '  name: pdf-agent-dispatcher' '  annotations:' \
	  '    iam.gke.io/gcp-service-account: $(call GSA,dispatcher)' '---' \
	  'apiVersion: v1' 'kind: ServiceAccount' 'metadata:' '  name: pdf-agent-stats-adapter' '  annotations:' \
	  '    iam.gke.io/gcp-service-account: $(call GSA,stats-adapter)' > params.yaml && \
	cp kustomization.yaml kustomization.yaml.bak && \
	kustomize edit set image \
	  pdf-agent-worker=$(REGISTRY)/pdf-agent-worker:$(TAG) \
	  pdf-agent-dispatcher=$(REGISTRY)/pdf-agent-dispatcher:$(TAG) \
	  pdf-agent-stats-adapter=$(REGISTRY)/pdf-agent-stats-adapter:$(TAG) && \
	kustomize build . | kubectl apply -f - ; \
	status=$$?; mv kustomization.yaml.bak kustomization.yaml; exit $$status

## Submit API on Cloud Run (IAM-authenticated).
api: check
	$(eval TAG := $(shell cat .last-image-tag 2>/dev/null || echo $(TAG)))
	gcloud run deploy pdf-agent-submit-api \
	  --image $(REGISTRY)/pdf-agent-submit-api:$(TAG) \
	  --service-account $(call GSA,submit-api) \
	  --set-env-vars ANTHROPIC_AGENT_ID=$$ANTHROPIC_AGENT_ID,ANTHROPIC_ENVIRONMENT_ID=$$ANTHROPIC_ENVIRONMENT_ID,INPUTS_BUCKET=$(INPUTS_BUCKET),OUTPUTS_BUCKET=$(OUTPUTS_BUCKET) \
	  --set-secrets ANTHROPIC_API_KEY=$(SECRET_API_KEY):latest \
	  --no-allow-unauthenticated --timeout 3600 --memory 1Gi --max-instances 10 \
	  --region $(REGION) --project $(PROJECT_ID)
	@for m in $$(echo "$$SUBMIT_API_INVOKERS" | tr ',' ' '); do \
	  gcloud run services add-iam-policy-binding pdf-agent-submit-api --member "$$m" \
	    --role roles/run.invoker --region $(REGION) --project $(PROJECT_ID) >/dev/null && echo "invoker: $$m"; \
	done
	@echo "SUBMIT_API_URL=$$(gcloud run services describe pdf-agent-submit-api --region $(REGION) --project $(PROJECT_ID) --format 'value(status.url)')"

## Create or update the agent (uses ANTHROPIC_API_KEY locally).
agent:
	python3 setup/anthropic_setup.py agent

status: creds
	kubectl -n $(NAMESPACE) get sandboxwarmpool,sandboxclaims,pods

## Prove sandboxes can reach only api.anthropic.com. Run after every deploy.
verify-egress: creds
	NAMESPACE=$(NAMESPACE) scripts/verify_egress.sh

## Stop all pods (Autopilot bills per pod). New sessions wait in Anthropic's
## queue until `make resume`. Cloud Run already scales to zero on its own.
pause: creds
	-kubectl -n $(NAMESPACE) scale deploy/pdf-agent-stats-adapter --replicas 0 2>/dev/null
	kubectl -n $(NAMESPACE) scale deploy/pdf-agent-dispatcher --replicas 0
	kubectl -n $(NAMESPACE) patch sandboxwarmpool pdf-agent-worker --type merge -p '{"spec":{"replicas":0}}'

resume: creds
	kubectl -n $(NAMESPACE) patch sandboxwarmpool pdf-agent-worker --type merge -p '{"spec":{"replicas":1}}'
	kubectl -n $(NAMESPACE) scale deploy/pdf-agent-dispatcher --replicas 1
	-kubectl -n $(NAMESPACE) scale deploy/pdf-agent-stats-adapter --replicas 1 2>/dev/null

destroy: check
	cd terraform && terraform destroy
