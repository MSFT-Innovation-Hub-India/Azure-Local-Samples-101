# PDF assistant: implementation and reproducible runbook

For domain definitions, prompts, content packages and UI scenario selection, see
[the scenario authoring and publication guide](SCENARIOS.md).

## How the solution was created and deployed

This was an additive deployment, not a replacement of the lab:

1. Inspected the existing Arc context, Foundry model, Services, certificate CA,
   storage class, node taints and resource reservations. The original worker did
   not have sufficient unreserved memory, so deployment stopped without writes.
2. The user added a second worker in the portal. Verified node/CSI/networking
   readiness, available reservations and continued Qwen inference before proceeding.
3. Implemented the shared FastAPI/React application, explicit local embedding
   service, bounded PDF indexer, persistent Qdrant and conversational retrieval.
   Added dependency locks, unit/integration tests and restricted Kubernetes templates.
4. Validated the code in isolated development dependencies. Laptop preview was
   explicitly marked development-only, not presented as an appliance deployment.
5. Attempted the approved lab ACR creation. Azure denied registry creation and
   the provider was unregistered. No role escalation/provider workaround was used.
   The user approved an existing registry in another tenant/subscription instead.
6. Used an ignored, isolated CLI profile to authenticate to that registry.
   Built the application/indexer and offline embedding images with ACR Tasks,
   imported pinned Qdrant and recorded immutable digests. No local Docker ran.
7. Used the guarded deployment script to check ownership, storage and capacity,
   copy model key/CA in memory, create the assistant's own credentials, and apply
   only `assistant-demo` resources. Existing Foundry/model resources were untouched.
8. Diagnosed the lab's host-network NodeLocal DNS interaction with Cilium.
   Configured only owned assistant pods to use the existing CoreDNS upstream
   Service; cluster DNS and general egress remained unchanged.
9. Uploaded synthetic PDFs to the document PVC and ran an in-cluster indexing Job.
   Verified retrieval, sources, follow-ups, reset, unsupported questions, content
   updates/deletions, network restrictions and persistence after pod replacement.
10. Extended the same engine with scenario packages. Definitions/prompts and PDFs
    are published separately from code; each scenario gets an immutable snapshot
    and atomic catalog entry. Only the core app image needs upgrading once.

The sections below retain the exact initial-deployment commands, prerequisites,
failures, fixes and validation boundaries. The [scenario guide](SCENARIOS.md)
documents how to add subsequent domains without rebuilding or replacing the core.

## Status and safety boundary

Implementation began on November 11, 2026 (UTC). **The assistant is deployed in
`assistant-demo` on Azure Local.** The app, embeddings, documents helper and
Qdrant are Ready on the added worker; both PVCs are Bound. The synthetic PDF was
indexed inside the cluster into two chunks. A browser request through the cluster
tunnel returned "The service recovery code is mango" with a page-1 source.

After the user increased `systempool` from one to two workers in the Azure
portal, read-only verification confirmed:

- Context: `zines-22325-aks`; existing model: `qwen2-5-0-5b-cpu`.
- One control-plane node and two workers, all Ready, with 8 reported CPU and
  approximately 15,862 MiB allocatable memory each. The Azure pool's configured
  SKU remains `Standard_D4s_v3`; the control plane has a `NoSchedule` taint.
- Original worker memory requests: **15,250 MiB**, leaving **612 MiB**.
- New worker memory requests: **219 MiB**, leaving **15,643 MiB** unreserved.
- New worker networking, DNS, kube-proxy and CSI node pods are Running/Ready.
  No node reports memory, disk, or PID pressure or unavailable networking.
- The scale operation reports `Succeeded`, existing Foundry pods remain Ready
  on the original worker, and both existing PVCs remain Bound.
- A post-scale, authenticated, TLS-verified Qwen request returned
  "The capital of France is Paris." The temporary verification tunnel was stopped.
- The Metrics API is unavailable. Requests are reservations, not measured usage.
- The default StorageClass uses `azlocal.csi.azure.com`.
- No ACR was returned by `az acr list` in the selected subscription.
- The user subsequently approved ACR creation in `zines-22325-rg`. Creating
  `ssrikantan22325acr` was attempted but denied with `AuthorizationFailed`:
  the current identity lacks `Microsoft.ContainerRegistry/registries/write`
  on that resource-group scope. No registry was created.
- A subsequent read-only provider check confirmed `Microsoft.ContainerRegistry`
  is `NotRegistered` in the lab subscription. Provider registration requires
  subscription-level authorization, independently of resource-group permissions.
- The user then explicitly approved an existing ACR in another subscription and
  tenant. Sign-in succeeded using an isolated CLI profile. Two remote ACR Tasks
  builds and the pinned Qdrant import succeeded under
  `ssrikantan/azure-local-pdf`; immutable references are in ignored
  `.local\acr-images.json`.
- No new registry, registry identity, role assignment, admin setting or registry
  network change was made. Existing lab resources were preserved; only owned
  resources in `assistant-demo` were deployed.
- Scenario support was upgraded through ACR run `ct3d` without rebuilding the
  embedding image or importing Qdrant again. The UI now lists the original
  operations guide, manufacturing maintenance and retail returns. Each added
  scenario has its own prompt, published revision and vector snapshot.
- Pull access currently uses a short-lived Entra token (approximately three
  hours), not a repository-scoped production identity. Refresh before future
  image pulls. Running containers do not use that token for inference.

The new stack reserves 2,496 MiB including one indexing Job, plus a deployment
guard requiring another 1,024 MiB of headroom on one eligible worker. **The new
worker passes this capacity check.** Do not lower requests, change Foundry
resources, use the control plane, or bypass the guard. Image pulls and volume
provisioning have now succeeded for the deployed images and PVCs.

**Image builds use Azure Container Registry Tasks (`az acr build`), not local
Docker.** No local Docker installation is required or was performed. An approved
ACR with build/import/push permissions and a pull mechanism is required.
The explicitly approved external registry supplies these images; the lab
subscription's provider-registration restriction does not prevent image pulls.
Do not assign access roles, use an unapproved subscription, or modify baseline
resources to work around an authorization denial.

## Architecture

```text
Laptop browser
  -> localhost:8080 -> kubectl port-forward -> Azure Arc Cluster Connect
  -> assistant-web ClusterIP Service
  -> one FastAPI pod, also serving the built React application
       -> embeddings ClusterIP Service -> local CPU ONNX embedding model
       -> qdrant ClusterIP Service -> persistent vector/text/metadata storage
       -> existing Foundry Qwen HTTPS Service -> local model inference

PDF source PVC -> indexing Job -> embedding service -> new Qdrant snapshot
                                                       |
                                        atomic active-alias switch
```

Initial ACR builds download public dependencies and model assets in Azure.
Runtime model files are baked into the embedding image; the embedding service
loads them offline. Kubernetes NetworkPolicy restricts runtime egress to DNS,
the local vector/embedding services, and the selected Qwen pods. Validate policy
enforcement with the lab's CNI after deployment.

This lab uses host-network NodeLocal DNS at `169.254.20.10`. A namespace-selector
DNS rule did not allow that host identity, and a DNS-only `/32` exception also
failed under the lab's Cilium configuration. Direct queries to the existing
`kube-system/kube-dns-upstream` Service succeeded. The deployment tool therefore
reads that Service's cluster IP and sets only the assistant pods' `dnsPolicy:
None`, nameserver and search domains to use CoreDNS directly. It does not alter
cluster DNS or permit general host/internet egress. Indexing Jobs must inherit
these settings as shown in Step 6. In a different cluster, select the appropriate
CoreDNS Service using `--dns-service`; this implementation assumes `cluster.local`.

The connected Arc browser tunnel is cloud-mediated. This is not a disconnected
deployment, even though the deployed application data processing is local.
Never include customer PDFs, credentials, or kubeconfigs in the ACR build context.
The allowlist in [`.dockerignore`](../.dockerignore) includes application sources,
frontend sources, the recipe, and dependency manifests only.

### Workloads and storage

| Workload | Memory request/limit | Role |
|---|---:|---|
| `assistant-web` | 384 MiB | React assets, FastAPI, retrieval, process-local chat sessions |
| `embeddings` | 1,024 MiB | MiniLM CPU text embeddings, 384 dimensions |
| `qdrant` | 512 MiB | Single-instance vector database |
| `documents` | 64 MiB | Small document-transfer helper mounting the PDF PVC |
| Indexing Job | 512 MiB | Bounded PDF extraction and snapshot publishing |

The helper is not another inference service. It provides a stable target for
`kubectl cp`. The indexer is colocated with it through pod affinity because their
shared PDF PVC is ReadWriteOnce. The deployment script pins the demo to a worker
that passes the capacity check, leaving the baseline worker alone when a new
worker is available.

PDF storage requests 2 GiB; vector storage requests 5 GiB. Both use filesystem
volumes provisioned by Azure Local CSI. Do not host Qdrant's live data directory
on the SMB/NFS document share. PVC deletion can delete the underlying volume:
the inspected StorageClass has reclaim policy `Delete`.

## Step 1 - Read-only environment checks

From the project directory, after signing in to the intended lab subscription:

```powershell
az account show --query "{subscription:id,tenant:tenantId}" -o json
az resource list --resource-group zines-22325-rg -o table
az acr list --query "[].{name:name,resourceGroup:resourceGroup,loginServer:loginServer}" -o table
kubectl config current-context
kubectl get nodes -o wide
kubectl describe nodes
kubectl get storageclasses
kubectl get pvc --all-namespaces
kubectl get pods --all-namespaces -o wide
kubectl top nodes
```

`kubectl top nodes` failed with `Metrics API not available`; no metrics extension
was installed as a workaround. `describe nodes` and the deployment guard account
for scheduled resource requests, including native sidecars.

If needed, keep the existing Arc connection in a separate terminal:

```powershell
az connectedk8s proxy --resource-group zines-22325-rg --name zines-22325-aks
```

Run the additive deployment tool in read-only mode:

```powershell
python -m tools.deploy_assistant
```

Originally this exited nonzero with the capacity explanation before any writes.
After scaling, it selects the new worker and proceeds to the next prerequisite:
the three immutable ACR image references shown in Step 5. Without these it
intentionally stops without reading model keys or writing resources. A passed
reservation check is not a load test or a guarantee against node memory pressure.

Commands used to verify the user-performed scale operation:

```powershell
az aksarc nodepool show --cluster-name zines-22325-aks --resource-group zines-22325-rg --name systempool -o json
kubectl get nodes -o wide
kubectl get pods --all-namespaces -o wide
kubectl get pvc --all-namespaces
kubectl get events --all-namespaces --field-selector type=Warning --sort-by=.lastTimestamp
```

The new node initially emitted image-filesystem and Multus startup warnings.
Its networking/CSI pods subsequently became Ready and node pressure conditions
are False. Existing Istio HPA metrics warnings remain because the Metrics API
is unavailable; no baseline extension was changed to address them.

## Step 2 - Isolated application dependencies and tests

Python 3.12 is the validated development interpreter. This does not change the
original standard-library client, which retains its existing requirements.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -m unittest -v tests.test_scenarios tests.test_assistant tests.test_deployment tests.test_manifests test_chat
```

[`requirements.txt`](../requirements.txt) lists direct runtime dependencies.
[`requirements.lock.txt`](../requirements.lock.txt) records the resolved runtime
versions; development adds PDF-fixture generation and manifest tests. The
Windows-only `win32_setctime` dependency has a platform marker. Linux container
execution still requires an actual successful ACR build and runtime validation.

An initial attempt to install public PyPI's latest FastAPI/PDF versions failed
because the configured package mirror did not offer them. Versions were selected
from the configured feed and locked. Do not repair this by installing packages
into Azure CLI's bundled interpreter or changing the CLI extensions.

Optional frontend development validation, with Node 22.23.4 or compatible Node
22 installed (no Docker):

```powershell
node --version
npm.cmd --version
Push-Location frontend
try {
    npm.cmd ci
    npm.cmd run build
} finally {
    Pop-Location
}
```

If Node is missing, this reproduces the portable installation used during
implementation, without installing Docker or changing the machine-wide PATH:

```powershell
$version = "v22.23.4"
$archive = "node-$version-win-x64.zip"
New-Item -ItemType Directory -Force .tools | Out-Null
Invoke-WebRequest "https://nodejs.org/dist/$version/$archive" -OutFile ".tools\$archive" -UseBasicParsing
$sums = (Invoke-WebRequest "https://nodejs.org/dist/$version/SHASUMS256.txt" -UseBasicParsing).Content
$expected = (($sums -split "`n" | Where-Object { $_ -match [regex]::Escape($archive) }) -split '\s+')[0]
if ((Get-FileHash ".tools\$archive" -Algorithm SHA256).Hash -ne $expected) {
    throw "Node archive checksum mismatch."
}
Expand-Archive ".tools\$archive" -DestinationPath .tools
Remove-Item ".tools\$archive"
```

Set that directory on the current shell's PATH before the frontend commands:

```powershell
$node = (Get-ChildItem .tools -Directory -Filter "node-*-win-x64" | Select-Object -First 1).FullName
$env:PATH = "$node;$env:PATH"
```

The production frontend build runs inside ACR; local Node is optional for
development. The npm lockfile retains versions and integrity hashes but omits
registry-specific download URLs so it can also work in ACR's build environment.
No frontend assets are downloaded from a CDN at runtime.

## Step 3 - Development integration check (not the on-appliance deployment)

This optional check was initially used while the cluster lacked deployment capacity.
It runs PDF extraction, real embeddings, and Qdrant's persistent embedded
development engine **on the laptop**, while calling the existing remote Qwen.
The Kubernetes manifests instead use a separate Qdrant server and embedding pod.

Only synthetic documents are generated:

```powershell
.\.venv\Scripts\python.exe -m tools.make_demo_pdf
$env:MODEL_CACHE = Join-Path (Get-Location) ".local\models"
$env:HF_HUB_DISABLE_TELEMETRY = "1"
.\.venv\Scripts\python.exe -m assistant.embeddings
```

This provisioning command downloads the pinned public ONNX embedding snapshot.
Subsequent preview runs use the cached snapshot offline.

Keep the model forward in another terminal if it is not already running:

```powershell
kubectl port-forward svc/qwen2-5-0-5b-cpu -n foundry-local-operator --address 127.0.0.1 5000:5000
```

Then:

```powershell
.\.venv\Scripts\python.exe -m tools.preview_assistant --check-only
```

The check verifies the recovery-code question, a follow-up, and filename/page
metadata against the real Qwen model. It reads the key into memory and validates
the model certificate against the Foundry CA and service DNS identity, even
though its socket connects through localhost. It does not use `--insecure`.

For a temporary local UI preview:

```powershell
.\.venv\Scripts\python.exe -m tools.preview_assistant
# Open http://localhost:8080; Ctrl+C stops the preview.
```

This is explicitly a development mode, not a fallback silently enabled in the
production application. It needs the ignored synthetic PDF/model-cache folders.

## Step 4 - Build all images in an approved ACR

**Original creation path was blocked by Azure RBAC.** The user subsequently
approved an existing external ACR, and the remote builds/import succeeded there.
The failed creation attempt is retained below for reproducibility; do not retry
it as a required deployment step.

The lab's naming convention requires a username prefix. The selected name is
`ssrikantan22325acr`: username prefix, lab number for uniqueness, and `acr`
suffix. Unlike other resource names, ACR names cannot contain hyphens.
`az acr check-name` reported this name available at the time of the attempt.

Commands performed:

```powershell
az group show --name zines-22325-rg --query "{name:name,location:location}" -o json
az acr check-name --name ssrikantan22325acr -o json
az acr create --resource-group zines-22325-rg --name ssrikantan22325acr --location eastus --sku Basic --admin-enabled false --tags owner=ssrikantan purpose=azure-local-pdf-assistant
az acr list --resource-group zines-22325-rg -o table
```

The RG location is `eastus`. The `create` command returned `AuthorizationFailed`
for `Microsoft.ContainerRegistry/registries/write`. A read-only effective
permission check found no matching allow for that action, and a subsequent
registry listing confirmed that no ACR was created. User approval does not grant
Azure RBAC permissions.

The additional provider-registration check was:

```powershell
az provider show --namespace Microsoft.ContainerRegistry --query "{namespace:namespace,registrationState:registrationState}" -o json
```

It returned `NotRegistered`; no registration was attempted. The lab administrator
must register `Microsoft.ContainerRegistry` at subscription scope and grant the
required creation permission at resource-group scope, or create this dedicated
registry and grant the necessary build/publish/import and scoped-pull-credential
permissions. Fixing only registration does not resolve the earlier write denial.
Do not enable the
admin account, assign yourself roles, change registry/network policy, use an
unapproved subscription, or create a new resource group as a workaround.

Authorized alternatives, if the lab administrator does not enable ACR creation:

- Use an existing shared ACR supplied by the lab team, with permissions limited
  to the assistant repositories and required build operations.
- Use an ACR in another explicitly approved subscription. Keep the Kubernetes
  resources in the existing lab cluster; build remotely in that registry and
  supply repository-scoped read-only pull credentials to the assistant pods.
  The lab subscription does not need its ContainerRegistry provider registered
  merely for kubelet to pull images over HTTPS from an existing external ACR.
- Verify organizational approval, registry/node network reachability, build
  access, and image-pull credentials before using either alternative. Do not
  switch subscriptions or transfer sources to another registry without approval.

These alternatives preserve ACR-only remote builds and local inference. No PDF
documents belong in the build context or registry; only code, dependencies and
model/image artifacts are published.

For an explicitly approved cross-tenant registry, isolate its sign-in from the
lab's existing CLI profile and subscription:

```powershell
$registry = "<approved-registry-name>"
$acrTenant = "<approved-registry-tenant-id>"
$acrSubscription = "<approved-registry-subscription-id>"
$acrProfile = Join-Path (Get-Location) ".local\acr-azure"
New-Item -ItemType Directory -Force $acrProfile | Out-Null
$previousConfig = $env:AZURE_CONFIG_DIR
try {
    $env:AZURE_CONFIG_DIR = $acrProfile
    az login --tenant $acrTenant --output none
    if ($LASTEXITCODE -ne 0) { throw "Registry sign-in failed." }
    az acr show --name $registry --subscription $acrSubscription --query loginServer -o tsv
    if ($LASTEXITCODE -ne 0) { throw "Approved registry is not accessible." }
}
finally { $env:AZURE_CONFIG_DIR = $previousConfig }

.\tools\build_acr.ps1 -Registry $registry `
    -Subscription $acrSubscription -AzureConfigDir $acrProfile `
    -RepositoryPrefix "ssrikantan/azure-local-pdf"
```

The script performs the following operations, checks each exit code, and records
immutable image references in ignored `.local\acr-images.json`. It restores the
previous CLI profile on success or failure and passes the subscription to every
ACR command. The default repository prefix is `assistant`; a username-prefixed
path was used in the approved shared registry.

```powershell
az acr build --registry <registry> --file Dockerfile --target app --image <prefix>/app:<unique-tag> --platform linux/amd64 --no-logs --subscription <approved-subscription> .
az acr build --registry <registry> --file Dockerfile --target embeddings --image <prefix>/embeddings:<unique-tag> --platform linux/amd64 --no-logs --subscription <approved-subscription> .
az acr import --name <registry> --source docker.io/qdrant/qdrant@sha256:9bfc89f9914fc14876a88c05270685921a55b8877b254a98bcc1cc2f0a424cd2 --image <prefix>/qdrant:<unique-tag> --subscription <approved-subscription>
az acr repository show --name <registry> --image <prefix>/app:<unique-tag> --subscription <approved-subscription> --query digest -o tsv
```

These illustrative commands also require the isolated registry CLI profile;
the build script sets/restores it automatically when given `-AzureConfigDir`.

On this Windows CLI, streaming Vite's checkmark character caused a CP1252
`UnicodeEncodeError` locally even though the remote build continued. Setting
`PYTHONIOENCODING` did not fix that CLI stream. The script now uses `--no-logs`,
which still waits for build completion and checks failure status. If a streamed
invocation fails locally, inspect the existing run before submitting another:

```powershell
# Use the isolated registry profile and explicit subscription for these too.
az acr task show-run --registry <registry> --run-id <run-id> --subscription <approved-subscription> --query "{status:status,images:outputImages}" -o json
```

The successful remote runs were `ct3b` (app) and `ct3c` (embeddings); their
existing results were reused, not rebuilt after the local stream error. Qdrant
was then imported and all three digest references recorded. The context allowlist
excludes local environments, credentials, documents, frontend dependencies and
generated output; the tightened second upload was about 18 KiB.

The embedding image includes the pinned model revision
`d13954661f83248295ba75c1ed411eef3b7b936e`. Its runtime does not download weights.
The application image is also the indexer's image. The Qdrant mirror ensures all
three runtime images come from ACR. Base images/dependency downloads at build
time still need permitted outbound access from ACR Tasks.

Resolve and record base image digests, scan the final images, and verify model
licenses before promoting a production release. The recipe's base tags are not
an immutable software-supply-chain guarantee.

### Layout of `deploy/`

| File | What it is | State in the cluster |
| --- | --- | --- |
| `qwen2.5-0.5b.yaml` | Model `qwen2-5-0-5b-cpu` (lab-provisioned; record of the live spec) | Running; the assistant's current model |
| `qwen2.5-14b.yaml` | Model `qwen2-5-14b-cpu`, pinned to `slmnodepool` | Running |
| `gpt-oss-20b.yaml` | Model `gpt-oss-20b-cpu`, pinned to `slmnodepool` | Removed (too slow on CPU); manifest kept for a future GPU pool |
| `stack.yaml`, `index-job.yaml` | Assistant application stack and indexing Job (`assistant-demo`) | Running; referenced by `tools/deploy_assistant.py` and the tests, so not renamed |

Model manifests are named `<family>-<size>.yaml`, and each one's `metadata.name`
is the ModelDeployment name used in its endpoint and API-key Secret.

### Optional GPT-OSS CPU evaluation on a separate worker

The lab owner added `slmnodepool` with one `Standard_D16s_v3` worker. Read-only
checks confirmed it Ready, reporting 32 allocatable CPUs and about 62.7 GiB
allocatable RAM, with 415m CPU and 219 MiB reserved by platform pods. These are
the node-reported values, not the portal SKU's advertised CPU count. Existing
Qwen and assistant workloads remained running.

[`deploy/gpt-oss-20b.yaml`](../deploy/gpt-oss-20b.yaml) is a separate, pinned
CPU/ONNX model definition targeting only this pool. Its initial evaluation
reservation is 8 CPU / 24 GiB, with limits of 16 CPU / 48 GiB. Those are evaluation
settings, not measured peak requirements or a throughput guarantee. Do not lower
them to force scheduling. Verify current reservations and available disk capacity
before applying; the catalog reports about 12.26 GiB of CPU model artifacts.
The catalog's short license field says MIT while its description identifies
Apache-2.0; review the actual asset license before production use.

**Deployment boundary:** the installed operator runs with
`--namespace=foundry-local-operator` and `ALLOWED_NAMESPACES=foundry-local-operator`.
It will not reconcile a ModelDeployment in `assistant-demo`. The prepared
manifest therefore names the operator's namespace and requires an authorized
platform administrator; it is not part of the assistant-only guarded deployment.
Do not change operator scope, weaken the assistant namespace's restricted Pod
Security policy, or patch generated pods to work around that boundary.

Creating this model must not replace the existing Qwen model or change the
assistant's configured endpoint. After an administrator deploys it, verify
model readiness, actual placement on `slmnodepool`, model-store free space, API
key creation, TLS and real chat completions. Never print API keys. Benchmark
generation with the same retrieved warranty evidence and check output/reasoning
handling before any application switch. GPT-OSS may need more than the current
256-token application output budget.

For recovery, keep the current Qwen configuration and all document/Qdrant PVCs.
An authorized administrator can remove only the newly created evaluation
ModelDeployment if necessary; do not delete the namespace, shared model store,
node pool or existing deployments.

**Deployment result (lab owner authorized, `slmnodepool` re-provisioned):**
`kubectl apply -f deploy/gpt-oss-20b.yaml` created `gpt-oss-20b-cpu`. The model
artifact downloaded in about 10 minutes and the model loaded in about 5 more;
the deployment reports `Running`/ready, with its pod placed on `slmnodepool`.
Qwen (`qwen2-5-0-5b-cpu`) and all `assistant-demo` workloads stayed Running and
untouched; the assistant still points at Qwen. The internal endpoint is
`https://inference-internal-gateway-istio.foundry-local-operator.svc.cluster.local:443/gpt-oss-20b-cpu`
and its keys are in Secret `gpt-oss-20b-cpu-api-keys` (never print them).

**Measured limitation: not usable for interactive chat.** Authentication and
routing work, and the model returns real tokens, but CPU generation is far too
slow: about 30 tokens took 170-300 s, with negligible cgroup throttling. The
operator's nginx sidecar returns 504 after 60 s, so non-streaming requests via
the gateway time out. gpt-oss also emits an `analysis` reasoning channel before
the answer, which consumes the output budget. Do not switch the assistant to it
on this CPU pool. A GPU pool (the catalog has `gpt-oss-20b-cuda-gpu`; the cluster
currently has no NVIDIA device plugin or `nvidia.com/gpu` resources) is the route
for GPT-OSS; do not raise limits or edit the operator's sidecar to compensate.
`gpt-oss-20b-cpu` was therefore removed (only that ModelDeployment) and replaced
by the evaluation below.

### Qwen2.5-14B CPU evaluation (alongside the existing 0.5B model)

[`deploy/qwen2.5-14b.yaml`](../deploy/qwen2.5-14b.yaml) deploys
`qwen2-5-14b-cpu` (catalog asset `qwen2.5-14b-instruct-generic-cpu` v4, about
11.06 GiB) pinned to `slmnodepool`, reserving 8 CPU / 16 GiB with limits of
16 CPU / 32 GiB. It was applied by the lab owner's authorization; the 0.5B
`qwen2-5-0-5b-cpu` deployment and all `assistant-demo` workloads were left
untouched at that point. The assistant now offers both models; see below.

Measured on the 32-CPU D16s_v3 worker: the artifact downloaded in about 9
minutes and the pod became ready about 2 minutes later. A short completion
through the internal gateway returned HTTP 200 (84 completion tokens in about
7.3 s, roughly 11 tokens/s) with a coherent answer; direct streaming measured
about 6 tokens/s on a very short reply. These are single-request, unloaded
numbers, not a capacity guarantee, and have not yet been run with the
assistant's retrieved-evidence prompts. Qwen2.5-14B's endpoint is
`https://inference-internal-gateway-istio.foundry-local-operator.svc.cluster.local:443/qwen2-5-14b-cpu`
with keys in Secret `qwen2-5-14b-cpu-api-keys` (never print them). Evaluate
answer quality and latency on the warranty scenario before switching the
assistant's model endpoint.

**Recovery:** an administrator can remove only the evaluation deployment
(`kubectl delete modeldeployment qwen2-5-14b-cpu -n foundry-local-operator`) to
free the node. Do not delete the namespace, model store, node pool or the 0.5B
Qwen deployment. If you remove it, redeploy the assistant with only
`--model-deployment qwen2-5-0-5b-cpu` (see below) so the drop-down no longer
lists it.

### Choosing between models in the assistant

The assistant offers every model listed in the `assistant-models` ConfigMap
(`models.json`, mounted at `/run/models`) in a **Model** drop-down. The first
entry is the default (the 0.5B model). The backend sends the request to the
selected model's own Service and API key; URLs and keys never reach the browser,
and history is kept separately per model. Without that file the app falls back to
its single legacy model settings, so older deployments keep working.

Deployed with the guarded tool, repeating `--model-deployment` once per model
(first = default). Use `--preserve-placement` for upgrades so that Qdrant, the
document volume and the other workloads stay on their current node; it is
refused if any CPU or memory request would change, so it cannot be used to
bypass the capacity guard:

```powershell
$dockerJson | .\.venv\Scripts\python.exe -B -m tools.deploy_assistant `
    --app-image $images.app_image --embeddings-image $images.embeddings_image `
    --qdrant-image $images.qdrant_image --registry-config-stdin --preserve-placement `
    --model-deployment qwen2-5-0-5b-cpu --model-deployment qwen2-5-14b-cpu --apply
```

This copies each selected model's API key into `assistant-credentials` as
`model-key-<deployment>` (never printed), keeps `model-key` for the default, and
widens the assistant's egress policy to exactly the listed model pods on ports
5000/8443. Without `--preserve-placement` the tool re-runs the capacity guard,
which may pick a different node; do not use that for an already-running
assistant. As with any model key rotation, rerun this and restart only
`deployment/assistant-web`.

**Verified:** the same warranty question on the Contoso scenario returned HTTP
200 from both models with identical retrieved sources. The 0.5B model answered in
about 3.5 s with a terse single-region answer; the 14B model took about 28 s
(about 1,000 prompt tokens, 95 output tokens) and gave a region-aware answer
with citations. These are single, unloaded requests, not a benchmark. Unknown
model ids return 404. The assistant processes one request at a time, so a slow
14B answer blocks others (they receive an explicit "busy" error).

### Subsequent core upgrades: rebuild only the application

Per-answer token usage display is a core API/UI change: use this app-only
ACR build/deployment path, then reload the browser. The backend forwards real
model-reported input/output/total token counts; missing usage is shown as
unavailable, and no-evidence responses explicitly indicate no model call.
Counts include the supplied prompt/history/excerpts and exclude embedding
tokenization. They are neither billing data nor evidence of answer correctness.
See the [scenario API guide](SCENARIOS.md#api-and-verification) for the response
contract. Changing only a scenario's sample questions remains a content release.

The scenario extension changed the app/indexer and frontend, not the embedding
model or Qdrant image. The following is the repeatable app-only path, using the
approved registry/profile variables from Step 4:

```powershell
if (-not (Test-Path .\.local\acr-images-before-scenarios.json)) {
    Copy-Item .\.local\acr-images.json .\.local\acr-images-before-scenarios.json
}
$images = Get-Content .\.local\acr-images.json -Raw | ConvertFrom-Json
$tag = "scenarios-" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
$previousConfig = $env:AZURE_CONFIG_DIR
try {
    $env:AZURE_CONFIG_DIR = $acrProfile
    az acr build --registry $images.registry --subscription $acrSubscription `
        --file Dockerfile --target app --platform linux/amd64 --no-logs `
        --image "$($images.repository_prefix)/app:$tag" --output none .
    if ($LASTEXITCODE -ne 0) { throw "Core application build failed." }
    $digest = az acr repository show --name $images.registry `
        --subscription $acrSubscription --image "$($images.repository_prefix)/app:$tag" `
        --query digest -o tsv
    if ($LASTEXITCODE -ne 0 -or $digest -notmatch '^sha256:[0-9a-f]{64}$') {
        throw "Cannot resolve the new immutable application digest."
    }
    $server = $images.app_image.Split("/")[0]
    $images.app_image = "$server/$($images.repository_prefix)/app@$digest"
    $images | ConvertTo-Json | Set-Content -Encoding ascii .\.local\acr-images.json
}
finally { $env:AZURE_CONFIG_DIR = $previousConfig }
```

Then rerun Step 5's in-memory pull-token/guarded-deployment block with the updated
image JSON. This upgrades the app and document helper, preserving the existing
PVCs, Qdrant, embedding weights and Foundry model. Check rollout before publishing
scenario packages. Content-only releases subsequently use the
[scenario publication tool](SCENARIOS.md), not another image build.

The ignored `acr-images-before-scenarios.json` preserves the old image references
for an explicitly chosen core rollback. A rollback of core code does not erase
scenario data, but the older UI cannot select it; do not delete PVCs or reset the
database to roll back application code.

## Step 5 - Registry pull access and guarded deployment

**Deployed and verified:** images pulled from the approved cross-tenant ACR,
and both Azure Local PVCs bound. Durable production pull identity is still
outstanding; the demo uses an expiring Entra token as described below.
An Azure-hosted AKS `--attach-acr` recipe should not be assumed to apply here.

Use the organization's supported pull identity where available. Otherwise,
obtain a short-lived repository-scoped read-only token for just
`assistant/app`, `assistant/embeddings`, and `assistant/qdrant`. Do not use an
ACR admin password. If creating an ACR scope map/token is necessary, that also
requires registry-owner approval; it is not performed by this project.

The deployment tool accepts Docker-format registry credential JSON from stdin
or a local file. **This is a credential format, not a requirement for local
Docker.** It validates the selected registry and a nonempty base64 `auth` entry.
Never paste passwords into command arguments, logs, or this runbook.

For this connected demo, the existing approved sign-in can provide an Entra
token without creating a registry identity or using an admin password:

```powershell
$previousConfig = $env:AZURE_CONFIG_DIR
try {
    $env:AZURE_CONFIG_DIR = $acrProfile
    $loginJson = az acr login --name $registry --subscription $acrSubscription `
        --expose-token --only-show-errors -o json
    if ($LASTEXITCODE -ne 0) { throw "Cannot obtain registry pull credentials." }
    $login = $loginJson | ConvertFrom-Json
    if (-not $login.accessToken -or -not $login.loginServer) {
        throw "Registry returned an invalid login response."
    }
}
finally { $env:AZURE_CONFIG_DIR = $previousConfig }
$auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
    "00000000-0000-0000-0000-000000000000:$($login.accessToken)"
))
$dockerJson = @{auths=@{$login.loginServer=@{auth=$auth}}} |
    ConvertTo-Json -Depth 5 -Compress
$images = Get-Content .\.local\acr-images.json -Raw | ConvertFrom-Json
$dockerJson | .\.venv\Scripts\python.exe -B -m tools.deploy_assistant `
    --app-image $images.app_image --embeddings-image $images.embeddings_image `
    --qdrant-image $images.qdrant_image --registry-config-stdin --apply
$deploymentExit = $LASTEXITCODE
$login = $loginJson = $auth = $dockerJson = $null
if ($deploymentExit -ne 0) { throw "Assistant deployment failed." }
```

This writes the pull Secret only in `assistant-demo`; no credential file is
created. The all-zero username is ACR's standard placeholder for an Entra token,
not a tenant/account ID. The token retains the sign-in identity's ACR permissions
and is **not repository-scoped least privilege**. It normally expires after
three hours. Rerun the in-memory login/deployment block before later image pulls
or when refreshing the Secret; no rebuild is needed. Cached images/running
containers are not a substitute for valid credentials after rescheduling.
Production requires an approved, dedicated pull-only identity and rotation.

Alternatively, an approved credential provider can write a temporary credential
JSON file outside source control:

```powershell
$images = Get-Content .\.local\acr-images.json -Raw | ConvertFrom-Json
# Set $registryConfig to the approved, temporary credential JSON path.
python -m tools.deploy_assistant `
    --app-image $images.app_image `
    --embeddings-image $images.embeddings_image `
    --qdrant-image $images.qdrant_image `
    --registry-config $registryConfig
```

That command is read-only. After approval and a successful preflight, repeat it
with `--apply`. If worker/node registry access is already configured, omit
`--registry-config`.

For another environment, use `--context`, `--storage-class`, `--dns-service`,
`--model-namespace`, `--model-deployment`, and `--model-ca-configmap`. The selected
model Service must provide the Foundry API on port 5000 with a trusted service
DNS certificate and its existing `<deployment>-api-keys` Secret.

The script refuses to overwrite an unowned namespace or resource. It creates
only owned resources in `assistant-demo`, copies the model key into that
namespace in memory, generates a separate Qdrant key, and copies the public CA.
No application pod receives Kubernetes API credentials.

The copied model key and CA are not automatically synchronized with Foundry.
After their rotation, rerun the guarded deployment with the same approved images
and restart only `deployment/assistant-web` in `assistant-demo` so its HTTP client
reloads them. Production needs automated secret/trust rotation.

After applying:

```powershell
kubectl get pvc -n assistant-demo
kubectl rollout status deployment/embeddings -n assistant-demo --timeout=300s
kubectl rollout status statefulset/qdrant -n assistant-demo --timeout=300s
kubectl rollout status deployment/assistant-web -n assistant-demo --timeout=300s
kubectl rollout status deployment/documents -n assistant-demo --timeout=300s
kubectl get pods -n assistant-demo -o wide
```

Do not treat Running/Ready as proof of end-to-end inference. Diagnose namespace
events and named workload logs if image pulls, mounts, probes, or TLS fail.
Do not change the baseline network, storage driver, or Foundry extension.

## Step 6 - Copy PDFs and create the first index

The first successful cluster Job indexed the synthetic fixture into two chunks.
Start with the generated synthetic fixture:

```powershell
.\.venv\Scripts\python.exe -m tools.make_demo_pdf
$pod = kubectl get pods -n assistant-demo -l app=documents -o jsonpath='{.items[0].metadata.name}'
kubectl cp .\.local\documents\. "assistant-demo/${pod}:/documents" -c documents
```

Finish uploads before starting the Job; do not mutate the source folder while
indexing. For customer data, use an approved read-only share mount instead of
copying through Arc if locality requirements prohibit that tunnel.

Create one Job at a time, copying the deployed app image and pull configuration:

```powershell
$active = kubectl get jobs -n assistant-demo -o json | ConvertFrom-Json
if (@($active.items | Where-Object { $_.status.active -gt 0 }).Count -gt 0) {
    throw "An indexing Job is already active."
}
$app = kubectl get deployment assistant-web -n assistant-demo -o json | ConvertFrom-Json
$job = kubectl create --dry-run=client --validate=false -f .\deploy\index-job.yaml -o json | ConvertFrom-Json
$job.spec.template.spec.containers[0].image = $app.spec.template.spec.containers[0].image
if ($app.spec.template.spec.imagePullSecrets) {
    $job.spec.template.spec | Add-Member -NotePropertyName imagePullSecrets -NotePropertyValue $app.spec.template.spec.imagePullSecrets
}
foreach ($setting in @("dnsPolicy", "dnsConfig")) {
    $value = $app.spec.template.spec.$setting
    if (-not $value) { throw "Missing deployed DNS setting: $setting" }
    $job.spec.template.spec | Add-Member -NotePropertyName $setting -NotePropertyValue $value
}
$name = $job | ConvertTo-Json -Depth 50 | kubectl create -f - -o name
kubectl logs -n assistant-demo $name --follow --pod-running-timeout=300s
kubectl wait -n assistant-demo --for=condition=complete $name --timeout=1800s
```

Check native command exit codes when scripting these commands. On failure,
inspect this Job's logs and status; the previous active index remains usable.

Index behavior:

- Text PDFs only; encrypted, corrupt, and textless PDFs fail explicitly. OCR is
  not implemented. Empty pages in otherwise text-bearing PDFs are skipped.
- Demo bounds: 200 PDFs, 25 MiB per PDF, 200 pages per PDF, 5,000 chunks total.
- Chunks contain up to 900 characters, with 120-character overlap, within pages.
  These are not exact token budgets. MiniLM is an English-focused model with
  input truncation; evaluate dense text, tables, languages, and chunk sizes.
- Unchanged corpus fingerprints do not create duplicate vectors.
- Changed/deleted PDFs produce a complete new snapshot; full re-embedding is
  intentional for this small demo, not a production incremental connector.
- The active alias changes only after all new vectors are stored. Failure
  before publication preserves the previous index. If a publication response is
  lost, the candidate is retained: inspect the alias rather than deleting a
  potentially active index. A killed Job may leave an unpublished candidate.
- Old snapshots are retained for rollback and consume disk. Retention/cleanup
  automation is not implemented. Remove only explicitly identified inactive
  collections after backup and verification, never all collections by wildcard.
- An empty source folder fails unless the indexer is deliberately run with
  `--allow-empty`, which publishes an empty index.

## Step 7 - Open the deployed application

The deployed application is available through the cluster Service:

```powershell
kubectl port-forward -n assistant-demo svc/assistant-web --address 127.0.0.1 8080:80
```

Open `http://localhost:8080`. Only this frontend/API endpoint is forwarded.
React uses same-origin `/api` requests; no browser key, separate frontend server,
public ingress, VM, or database/model port-forward is required.

Arc stream interruptions can end `kubectl port-forward` without stopping any
pods. Keep the Arc proxy active and restart this command if necessary. For a
demo terminal that reconnects explicitly:

```powershell
while ($true) {
    kubectl --context zines-22325-aks port-forward -n assistant-demo `
        svc/assistant-web --address 127.0.0.1 8080:80
    Write-Warning "Browser tunnel exited ($LASTEXITCODE); retrying in 3 seconds."
    Start-Sleep -Seconds 3
}
```

Ctrl+C stops the tunnel. It runs only the transport on the laptop, not the
assistant, embedding model, vector database or indexer.

Validate:

1. Index status shows the expected chunk count.
2. Ask "What is the service recovery code?" Expect mango and a page-1 source.
3. Ask "What is that recovery code again?" Verify follow-up context.
4. Ask a question the documents do not answer. Check refusal and source relevance.
5. Start a new conversation and check history is cleared.
6. Replace/delete a source PDF, rerun indexing, and confirm old text is no longer
   retrieved through the active alias.
7. With approval, restart only the assistant's named pods and confirm PVC data
   survives. In-memory chat sessions intentionally do not survive app restarts.
8. Test DNS, Qwen certificate verification, pull credentials, and network policy
   with the actual pods. DNS, TLS model calls, image pulls and internal service
   connectivity have now been verified.

The application retains six recent messages per session, up to 100 sessions,
with one-hour idle expiry. One model request runs at a time; concurrent requests
receive an explicit retry error. Answers are nonstreaming. No-evidence searches
return an explicit response without invoking Qwen. The similarity cutoff is a
demo heuristic, not a calibrated factuality guarantee. Returned references are
retrieved passages, not proof that every model assertion is supported.

Retrieval adds the last two user questions only for likely references such as
"its", "that", "again", or "what about". New-topic queries do not automatically
inherit unrelated old questions. This is a limited follow-up heuristic, not a
general-purpose query-rewriting model; evaluate customer conversations before
production use. Generation still receives the bounded conversation history.

## Customer production perspective

| Demo | Production work still needed |
|---|---|
| Existing small Qwen 0.5B | Evaluate answer quality, context/token budgets, CPU/GPU sizing, throughput |
| Shared demo corpus | User authentication and document ACL filtering before model context construction |
| Manually triggered indexing | Durable ingestion, connectors, OCR, retries, audit, deletions, index retention |
| Single app and Qdrant replicas | Distributed session store, HA placement, database replication and recovery |
| CSI PVCs | Capacity monitoring, tested backups/restores and disaster recovery objectives |
| Private HTTP vector/embedding Services | Threat-model-driven TLS/mTLS, secret rotation, tighter component policies |
| Loopback port-forward | Corporate private HTTPS access, internal DNS, identity, network controls |
| Connected Arc and ACR provisioning | Approved artifact import, local identity and updates for disconnected operation |

The namespace has restricted pod-security settings and a NetworkPolicy, but is
not a hard tenant security boundary. The demo has no user authentication or
document-level authorization: keep it loopback-only and use approved shared demo
documents. No tool execution or arbitrary PDF-content instructions are enabled.

## Validation record

Performed:

- **Token-display update:** 46 targeted assistant/scenario tests and the React
  production build passed. ACR run `ct3e` built the app-only upgrade; guarded
  deployment reused the embedding/Qdrant images and preserved all indexes.
- Contoso's published UI examples now ask single-document questions. Fresh-session
  live checks returned India 18 months / 5,000 hours, APAC 24 months, repair
  warranty 90 days, Tier 1 INR 25,000, and Tier 2 Regional Service Manager.
  These checks are not a guarantee of other answers or follow-up accuracy.
- Live browser counts matched the chat API's model-reported usage. For example,
  the India coverage question returned 1,177 input + 16 output = 1,193 tokens.
  Browser fixtures separately verified zero counts, unavailable usage, no-model
  responses, failed-request disclosure, and layout at 390-pixel width. Fixtures
  were removed after testing; these synthetic counts are not inference evidence.
- The final question-only publication completed as Job `pdf-index-wsntp`,
  retaining Contoso's original 13-chunk vector snapshot. Core rollback image
  references are preserved locally in ignored
  `.local\acr-images-before-token-usage.json`; use the guarded deployment path
  with those references if explicitly rolling back, without deleting storage.
- **Scenario extension: 69 regression tests and the React production build passed.**
  Coverage adds package/path/schema validation, per-domain collections/prompts,
  isolated histories/revisions, prompt-only updates, index failures and uncertain
  catalog publication, legacy migration and HTTP compatibility.
- ACR run `ct3d` built only the core app image; the embedding and Qdrant image
  digests remained unchanged. Guarded deployment preserved the original two-chunk
  operations index and the baseline Qwen pod (Ready, zero restarts).
- Manufacturing and retail packages were published by the new tool using
  in-cluster Jobs. All three scenarios were discovered through the actual cluster
  API and browser tunnel.
- Live Qwen answered manufacturing approval code **cobalt**, retail approval code
  **amber**, and operations recovery code **mango**. Cross-domain API follow-ups,
  scenario reset, source excerpts and unknown-ID rejection were verified.
- The browser selector changed examples/prompts/index context, cleared the prior
  transcript on switching and returned domain-correct answers. At viewport widths
  1280 and 390 pixels the selector stayed visible without horizontal overflow.
- The Qdrant pod was replaced after scenario publication; the catalog and all
  three active vector snapshots remained available. Publication locks were
  released after successful Jobs.
- **54 regression tests passed:** PDF extraction/page metadata,
  unchanged/changed/deleted corpus behavior, atomic publication on failure,
  persisted Qdrant reopen, session isolation/reset, failed-turn handling,
  follow-up retrieval/prompt context, input/CSRF checks, capacity thresholds,
  credential validation, kubectl JSON streams and CoreDNS wiring.
- React production build succeeded with pinned dependencies and an npm lockfile.
- Pinned Python runtime distributions were successfully resolved/downloaded for
  Linux x86-64/Python 3.12; this is not a substitute for running the final image.
- Real MiniLM embeddings plus live Qwen returned `mango` for the synthetic
  recovery question and its follow-up, with `demo-guide.pdf`, page 1.
- TLS validated using the Foundry CA and model Service DNS name.
- The real React page was exercised in the browser: question, follow-up, source
  expansion, reset, and an unsupported cricket question. The latter returned
  that no supporting information was found. Desktop/mobile widths had no
  horizontal overflow.
- Kubernetes client dry-run accepted both manifest files.
- The deployment guard refused the original single-worker capacity without
  writes. After the user added a second worker, its capacity check passed and
  it stopped at the missing ACR image references, again without writes.
- ACR Linux builds/import, immutable image pulls, restricted nonroot containers,
  four Ready services, two Bound PVCs and real in-cluster PDF indexing succeeded.
- The cluster-forwarded React UI returned the correct `mango` answer and a
  `demo-guide.pdf`, page-1 source through the internal, TLS-verified Qwen Service.
- The same browser conversation answered its follow-up with `mango`, expanded
  the original source passage, refused an unsupported cricket question, and
  cleared the transcript with New conversation. A fresh question then returned
  backups every six hours, retained fourteen days, with the correct page-2 source.
- Reindexing the unchanged corpus returned `unchanged`, two chunks. Adding a
  separate synthetic lifecycle PDF published a three-chunk snapshot; its marker
  was present in the active Qdrant collection. Deleting only that test PDF and
  reindexing returned to two chunks, with no deleted document in the active
  collection. The local and uploaded temporary validation PDFs were removed.
- Qdrant's pod was replaced after indexing; the same PVC retained its active
  alias and two chunks. The embedding pod also restarted under the restricted
  policy and produced a 384-dimensional vector from its baked-in model.
- From the documents pod, permitted Qdrant TCP/6333 was reachable, while the
  policy-disallowed application Service port and external `1.1.1.1:443` both
  timed out within the three-second test bound.
- The initial owned failed DNS indexing Job was removed after diagnosis and
  successful retries. Completed indexing Jobs remain as execution history.
- The baseline Qwen pod remains Ready with zero restarts; the default lab CLI
  subscription/tenant were unchanged by registry authentication and builds.
- The deployment tool now accepts both kubectl's concatenated JSON objects and
  a JSON List when rendering multi-document YAML.
- Startup warnings are visible: Qdrant cannot write its optional
  `.qdrant-initialized` indicator on the read-only root filesystem, and ONNX
  Runtime cannot persist a telemetry device ID there. Both services start
  successfully; neither warning is treated as proof of indexing failure.
  Qdrant telemetry is disabled; runtime egress remains restricted.

Not yet verified:

- Final image vulnerability scans and production supply-chain approval.
- Production load, availability, authentication/ACLs, OCR or disconnected operation.

The npm install advisory check reported zero vulnerabilities, but separate
audit endpoint calls were unreliable (configured feed HTTP 400; direct public
endpoint TLS failure). Do not interpret that as a completed independent security
assessment. FastAPI's current test stack emits an httpx deprecation warning;
the tests still pass. Neither issue was hidden by disabling verification.

## Related resources

- [Lab baseline and change boundaries](https://docs.almr.adaptivecloudlab.com/eng-22325/about/pre-deployed.html)
- [Existing Qwen architecture](../ARCHITECTURE.md)
- [Qdrant storage requirements](https://qdrant.tech/documentation/installation/)
- [ACR quick builds](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tutorial-quick-task)
- [ACR authentication and Entra token lifetime](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-authentication)
- [Foundry Local disconnected operation](https://learn.microsoft.com/en-us/azure/azure-sovereign-clouds/private/foundry-local/disconnected-operations/concept-overview)
