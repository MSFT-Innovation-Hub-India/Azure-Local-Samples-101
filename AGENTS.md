# Project safeguards

- Preserve the lab's existing resource groups, networks, Arc extensions, cluster,
  Foundry resources, Qwen deployment, and other users' workloads. Changes for the
  PDF assistant are additive and confined to the new `assistant-demo` namespace.
- Never delete resource groups, change baseline resource requests, scale cluster
  infrastructure, tolerate control-plane taints, or bypass capacity checks to
  make a demo fit. Report insufficient capacity and request lab-owner approval.
- Never commit credentials, tenant/account identifiers, kubeconfigs, private
  documents, or copied Secrets. Read model credentials into memory and create
  only the assistant's namespaced Secret; never print the values.
- Keep document parsing, embedding, retrieval, and inference on Azure Local.
  Initial image/model provisioning may require internet access; the connected
  Arc browser tunnel is not a disconnected or wholly on-premises access path.
- Serve the built React UI from FastAPI, using same-origin relative API URLs.
  Keep model and vector database credentials exclusively on the backend.
- Use a separate local embedding model, persistent PDF/vector storage, and
  source citations. Reject unusable PDFs explicitly and keep the previous
  searchable index intact if indexing fails.
- Document commands, prerequisites, validation, limitations, and safe recovery
  steps in [the assistant runbook](docs/ASSISTANT.md) as implementation proceeds.
- Use isolated application dependencies, not Azure CLI's bundled Python.
  For CLI import failures, check native Python ABI and cross-extension conflicts
  before changing cluster resources; preserve extension versions when repairing.
- Build and publish containers with `az acr build`, never local Docker. Use an
  approved ACR and mirror Qdrant there. Do not create a registry or change
  registry networking, identities, or lab infrastructure without approval.
- Preserve the existing standard-library chat demo and its tests.
