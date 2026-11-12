# Adding domain scenarios to the Azure Local assistant

## What stays shared

Every scenario uses the same React/FastAPI application, local embedding service,
Qdrant server and existing Foundry Qwen model. Adding documents or changing a
domain prompt does **not** rebuild images, restart the app, create another model,
or replace other scenarios.

A one-time core application upgrade adds scenario discovery and selection. Core
code changes still require an ACR build and guarded deployment; content-only
scenario releases use the package workflow below.

The original operations demo remains available as `operations`. Its existing
vector index and uploaded PDF are preserved when other scenarios are added.

**Verified deployment:** operations, manufacturing and retail are published on
the current Azure Local cluster. Identical approval-code questions returned
`cobalt` and `amber` in the respective domains; operations still returned `mango`.
All three indexes/catalog entries survived a Qdrant pod replacement. The 69
regression tests and React production build pass.

## Repository layout

```text
assistant/
  app.py                 HTTP API, sessions, CSRF, static React assets
  service.py             Shared retrieval and grounded model conversation
  clients.py             Shared model, embeddings and Qdrant clients
  indexer.py             Shared PDF extraction and vector snapshot indexing
  scenarios.py           Definition validation, package validation and catalog
frontend/src/
  main.jsx               Scenario selector, isolated chat, sources
scenarios/
  operations/
  manufacturing/
  retail/
tools/
  scenario.py            new / package / deploy
  make_demo_pdf.py        Synthetic demonstration documents
  build_acr.ps1          Core image builds in ACR, never local Docker
  deploy_assistant.py    Guarded core Kubernetes deployment
deploy/
  stack.yaml             Shared services and storage
  index-job.yaml         Reusable in-cluster indexing Job
```

Each scenario directory contains only:

```text
my-domain/
  scenario.json
  system-prompt.md
  documents/
    policy.pdf
    procedures/
      guide.pdf
```

The definition and domain prompt are configuration, not application code.
PDFs and generated packages are excluded from Git by the project ignore rules.
Do not commit confidential content in prompts or definitions either.

## 1. Create a scenario

From the project directory, with the existing Python environment:

```powershell
.\.venv\Scripts\python.exe -m tools.scenario new facilities `
    --name "Facilities support"
```

This creates `scenarios\facilities`. Edit its definition:

```json
{
  "schema_version": 1,
  "id": "facilities",
  "name": "Facilities support",
  "description": "Answers about approved facilities policies and support processes.",
  "example_questions": [
    "Who approves a facilities request?",
    "What is the support escalation process?"
  ]
}
```

IDs are stable lowercase slugs, up to 40 characters. Reusing an ID updates that
scenario; choosing a new ID adds another scenario. Folder names should match IDs
for clarity. Names allow 80 characters, descriptions 500, and up to six example
questions of 200 characters each. Unknown definition fields fail validation.

Edit `system-prompt.md`, for example:

```text
You assist facilities support staff. Focus on approval responsibilities,
service windows and escalation procedures. Explain acronyms clearly.
Use only the supplied policy excerpts and acknowledge missing information.
```

The prompt must contain 1-2000 characters. It supplements the shared grounding
instructions; retrieved documents are still treated as untrusted evidence.
Prompts are authored by the trusted scenario publisher and remain on the backend.
Do not use a prompt as a substitute for application authorization or safety controls.

Copy your text PDFs into `scenarios\facilities\documents`. Nested folders work.
Word documents must first be exported to PDF; scanned PDFs require OCR before
this pipeline can index them. Finish editing/copying files before packaging.

## 2. Package it

```powershell
$release = .\.venv\Scripts\python.exe -m tools.scenario package `
    .\scenarios\facilities | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Scenario packaging failed." }
$release.package
```

The output is a uniquely named ZIP in ignored `.local\scenario-packages`.
It contains only the definition, prompt and PDFs, not the repository, credentials,
Python environment or other files. The archive is a content release, **not a
container image**; customer PDFs do not go into ACR.

Bounds:

- 1-200 PDFs; each PDF at most 25 MiB and 200 pages.
- 5,000 extracted chunks per scenario.
- At most 48 MiB compressed and expanded content per package, fitting the
  indexer's existing 64 MiB temporary volume.
- Encrypted/corrupt/no-text PDFs fail; extraction and embeddings run in-cluster.
- Unsafe paths, symlinks, duplicate paths, unexpected ZIP entries and unsupported
  documents fail rather than being silently skipped.

## 3. Publish to the existing cluster

Keep the Arc proxy active in its own PowerShell window:

```powershell
az connectedk8s proxy --resource-group zines-22325-rg --name zines-22325-aks
```

Then publish:

```powershell
.\.venv\Scripts\python.exe -m tools.scenario deploy $release.package `
    --context zines-22325-aks
if ($LASTEXITCODE -ne 0) { throw "Scenario publication failed." }
```

The tool:

1. Validates the archive before cluster writes.
2. Checks namespace/resource ownership, Bound document storage and worker capacity.
3. Rejects another active indexing Job and takes an atomic namespaced publication lock.
4. Copies only the ZIP into the existing document PVC using the owned helper pod.
5. Creates a Job using the deployed immutable app image, existing pull Secret,
   verified CoreDNS settings, read-only documents and Qdrant-only credential.
6. Extracts bounded files into the Job's temporary volume on Azure Local.
7. Builds a complete, scenario-specific vector snapshot with the local embedding model.
8. Atomically publishes one Qdrant catalog record containing the scenario metadata,
   domain prompt, revision and **immutable vector collection name**.
9. Waits for Job completion, prints its result and releases the publication lock.

Only resources in `assistant-demo` are written. No Azure resource-group,
Foundry, registry, DNS infrastructure or baseline workload changes are made.

The connected upload uses Arc, which is cloud-mediated. For a strict disconnected
or data-locality deployment, replace this upload transport with an approved local
ingestion path; the current demo is not air-gapped.

## 4. Select and demonstrate

Use the existing application tunnel, or start one if none is running:

```powershell
kubectl --context zines-22325-aks port-forward -n assistant-demo `
    svc/assistant-web --address 127.0.0.1 8080:80
```

Open `http://localhost:8080`, click **Refresh scenarios**, and choose the domain
from **Use case scenario**. Its description, example questions, index status,
retrieval collection and domain prompt are selected together.

Switching scenarios starts a new conversation in the UI. The backend keys history
by browser session, scenario ID and published revision; old facts/questions never
become another domain's conversation history. Updating the selected scenario's
revision also starts a fresh backend conversation. Refresh clears stale displayed
history, and responses from a new revision replace rather than append old turns.

This is context isolation, **not access control**. Anyone who can reach this demo
can select any published scenario. Production needs user authentication and
document/scenario authorization before retrieval.

### Included demonstration scenarios

Generate synthetic documents, then package and publish each domain:

```powershell
.\.venv\Scripts\python.exe -m tools.make_demo_pdf --scenario manufacturing
.\.venv\Scripts\python.exe -m tools.make_demo_pdf --scenario retail

$manufacturing = .\.venv\Scripts\python.exe -m tools.scenario package .\scenarios\manufacturing | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Manufacturing packaging failed." }
.\.venv\Scripts\python.exe -m tools.scenario deploy $manufacturing.package
if ($LASTEXITCODE -ne 0) { throw "Manufacturing publication failed." }

$retail = .\.venv\Scripts\python.exe -m tools.scenario package .\scenarios\retail | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Retail packaging failed." }
.\.venv\Scripts\python.exe -m tools.scenario deploy $retail.package
if ($LASTEXITCODE -ne 0) { throw "Retail publication failed." }
```

| Selected scenario | Question | Expected document fact |
|---|---|---|
| Operations guide | What is the service recovery code? | mango |
| Manufacturing maintenance | What is the approval code? | cobalt |
| Manufacturing maintenance | How often are press inspections scheduled? | Every ten days |
| Retail returns | What is the approval code? | amber |
| Retail returns | How long is the return window? | Twenty-one days |
| Retail returns | Who can approve exceptions? | Store manager |

Ask the identical approval-code question in manufacturing and retail to
demonstrate separate knowledge, not just different UI labels. Expand sources.
These are fictional demonstration policies, not real operational instructions.

## Updating and recovering a scenario

Edit its PDFs, prompt or definition; package and deploy it again using the same ID.
No core image build is needed.

- Unchanged PDFs reuse the active snapshot; prompt/definition-only changes publish
  new metadata without re-embedding.
- Added/changed/deleted PDFs produce a complete new snapshot for only that scenario.
- The runtime catalog changes only after indexing succeeds. A failed release
  leaves the previous prompt/index pair available.
- A catalog write can commit despite a lost response. Treat a failed/timeout
  publication as uncertain: inspect the selected scenario before retrying.
  Published/candidate snapshots are retained, never deleted after an uncertain commit.
- Operations initially reads the original legacy alias. Its first packaged release
  pins the previous snapshot before advancing that alias. After migrating it to
  packages, use packages for its updates, not the old bare-folder indexing command.
- Old snapshots and uploaded ZIPs remain for recovery and consume storage.
  Retention/rollback automation is not implemented. Clean up only specifically
  identified inactive releases after validation and backup.

Useful diagnostics:

```powershell
kubectl get pods,jobs,pvc -n assistant-demo
kubectl get jobs -n assistant-demo --sort-by=.metadata.creationTimestamp
# Replace the value with the Job reported by the publication tool:
$job = "pdf-index-REPLACE"
kubectl logs -n assistant-demo "job/$job"
kubectl describe job $job -n assistant-demo
kubectl get configmap scenario-publication-lock -n assistant-demo -o yaml
```

If the publisher is interrupted, the Job can continue in-cluster. A running or
uncertain Job keeps the publication lock. **Do not remove the lock while a
publisher or unfinished Job is active.** Once the publisher has stopped and its
Job is confirmed Complete/Failed, the owned lock can be removed explicitly:

```powershell
kubectl delete configmap scenario-publication-lock -n assistant-demo
```

Serial publication is intentional: do not run manual legacy Jobs concurrently
with this workflow. The lock coordinates this tool's publishers, not arbitrary
cluster administrators. Kubernetes Job deadline is 30 minutes.

The demo has a 100-scenario metadata guard, not a guarantee of capacity for 100
large corpora. All collections share Qdrant's existing memory/disk budget;
uploaded packages share the document PVC. Monitor and size cumulative data,
snapshots, ingestion CPU and generation concurrency before expanding real workloads.
The small Qwen model and character-based prompt budget need domain evaluation.

## API and verification

```text
GET  /api/scenarios
GET  /api/status?scenario=retail
POST /api/chat             {"scenario":"retail","question":"..."}
POST /api/reset?scenario=retail
```

Chat/reset retain the existing CSRF requirement and session cookie. Omitting a
scenario retains backward compatibility with `operations`. Unknown IDs return
404; invalid IDs return 422, never a successful fallback into another domain.

Chat responses also include `model_called` and `usage`. When reported by the
inference endpoint, `usage` contains integer `prompt_tokens`, `completion_tokens`
and `total_tokens`. These are actual endpoint-reported counts, not estimates.
Input tokens include system/domain instructions, supplied conversation history,
retrieved excerpts and the question; they are not just the typed question.
The UI displays input, output and total counts below each answer.

When inference returns no usage metadata, `usage` is `null` and the UI explicitly
reports counts as unavailable. If retrieval finds no supporting passages,
`model_called` is `false`, `usage` is `null`, and the UI explains that no inference
tokens were used. Counts do not include embedding tokenization, are not a billing
record, and do not establish answer accuracy. Failed chat requests have no known
usage and are labeled accordingly.

```powershell
.\.venv\Scripts\python.exe -B -m unittest -q `
    tests.test_scenarios tests.test_assistant tests.test_deployment `
    tests.test_manifests test_chat
```

See [the core deployment runbook](ASSISTANT.md) for registry sign-in isolation,
ACR-only builds, expiring pull credentials, TLS, CoreDNS, initial deployment,
production gaps and safe infrastructure boundaries.
