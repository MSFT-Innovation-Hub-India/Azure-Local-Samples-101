# Qwen conversational chat

The Python client uses only the standard library; no packages or virtual
environment are required. Python 3.9 or newer and `kubectl` must be on PATH.
The client uses your current kubectl context.

## Architecture

![Qwen on Azure Local architecture](./architecture.svg)

See the [architecture walkthrough](./ARCHITECTURE.md) for the numbered request
path, important pods, model catalog and storage, and the distinction between the
active port-forward tunnel and the installed Istio/Envoy gateway route.
The diagram is available as [editable SVG](./architecture.svg) and [PNG](./architecture.png).

## Azure Local lab reference

This sample accompanies the **Azure Local Labs - AKS Arc with Foundry Local
(eng-22325)** lab:

[Run Inference | Azure Local Labs - AKS Arc with Foundry Local (eng-22325)](https://docs.almr.adaptivecloudlab.com/eng-22325/foundry-local/run-inference.html?tabs=powershell)

Use the lab guide and its preceding setup steps for the Azure Local, AKS Arc,
and Foundry Local prerequisites. Before running this Python demo, the Qwen model
deployment must be ready and its API key Secret available. The instructions below
cover connecting to that deployment and running the conversational client; they
do not replace the lab's infrastructure setup.

## Prerequisite: start the Arc proxy

Before running the demo, ensure Azure CLI is installed and signed in with access
to the Arc-enabled cluster. In the first PowerShell terminal, run:

```powershell
az connectedk8s proxy `
    --resource-group zines-22325-rg `
    --name zines-22325-aks
```

Wait until the command reports that the proxy is listening and that
`zines-22325-aks` is the current kubectl context. The proxy updates your kubeconfig
so subsequent kubectl commands can reach the cluster.

Leave this terminal running throughout the demo. Ctrl+C closes the proxy.

## Run

In a second PowerShell terminal, keep the port-forward running:

```powershell
kubectl port-forward svc/qwen2-5-0-5b-cpu -n foundry-local-operator 5000:5000
```

In a third terminal, from this folder:

```powershell
python .\chat.py --insecure
```

`--insecure` matches the `curl --insecure` in the original test. It disables
certificate verification: use it only with your trusted local port-forward.
Omit it if the endpoint has a certificate trusted by Python.

The script fetches the primary API key and resolved model name through kubectl.
It does not print or save the key. It sends each question with previous successful
user/assistant turns so you can ask follow-up questions.

- `/reset`: start a fresh conversation.
- `/history`: display saved user/assistant messages without calling the model.
- `/exit` or `/quit`: stop (Ctrl+C and end-of-input also stop).
- Failed turns are reported and not added to history; enter the question again
  to retry.

History lives only in memory and is lost on exit. Long conversations can exceed
the model's context limit; use `/reset` when needed. Responses are non-streaming,
so the assistant prints after the model finishes generating.

For slower CPU inference or longer responses:

```powershell
python .\chat.py --insecure --timeout 300 --max-tokens 512
```

`--timeout` sets the HTTP socket timeout in seconds (default: 180).
`--max-tokens` limits each response (default: 256), not the conversation length.
Use `python .\chat.py --help` for all options.

## Check conversation history

Keep the same Python process running for follow-up questions. Restarting the
script starts a new conversation; neither the client nor this API call saves a
conversation between runs.

To see the number and roles of messages sent with each request:

```powershell
python .\chat.py --insecure --debug
```

For example, enter these as two separate prompts at `You:`:

```text
My favorite fruit is mango. Remember this for my next question. Reply with OK.
What is my favorite fruit? Reply with only the fruit name.
```

The first request should show `Sending 2 messages: system, user`. The second
should show `Sending 4 messages: system, user, assistant, user`. Enter `/history`
to inspect the actual saved turns. Debug output does not print message content
or API keys; `/history` deliberately displays the conversation in your terminal.

If prior turns are present but the answer ignores them, that alone does not mean
the client dropped history. Model behavior and context limits can also affect
recall. The small 0.5B model may not reliably follow complex follow-up questions.

## Optional virtual environment

An environment is useful if you add dependencies later, but unnecessary now.
You can use one without activating it:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe .\chat.py --insecure
```

## Tests

Tests use mocks; they do not contact Kubernetes or the model or read real keys.

```powershell
python -B -m unittest -v test_chat
```

## Troubleshooting: Arc proxy cannot import `rpds.rpds`

If `az connectedk8s proxy` fails with `No module named 'rpds.rpds'` before
connecting, check `az --version` for the CLI's bundled Python version. A CLI
upgrade can leave an extension's compiled dependencies built for the previous
Python version. For example, an `rpds.cp313-win_amd64.pyd` file cannot load under
Python 3.14.

Reinstall the local extension to rebuild its dependency installation for the
current CLI interpreter:

```powershell
az extension remove --name connectedk8s
if ($LASTEXITCODE -ne 0) { throw "Could not remove connectedk8s." }

az extension add --name connectedk8s
if ($LASTEXITCODE -ne 0) { throw "Could not reinstall connectedk8s." }

az connectedk8s proxy --help
```

If the help command loads successfully, rerun the Arc proxy command above.
Reinstalling this extension does not remove the Arc registration or model
deployment. Installing `rpds-py` in the chat client's Python environment will not
repair Azure CLI: the CLI uses its own interpreter and extension dependencies.

The CLI can also load dependencies from another installed extension. If the next
traceback points into `cliextensions\aksarc` and reports a `pydantic_core` import
failure, reinstall that extension too:

```powershell
az extension remove --name aksarc
if ($LASTEXITCODE -ne 0) { throw "Could not remove aksarc." }

az extension add --name aksarc
if ($LASTEXITCODE -ne 0) { throw "Could not reinstall aksarc." }

az connectedk8s proxy --help
```

Use the extension directory named in the traceback to identify the conflicting
installation. Do not rename or copy compiled `.pyd` files between Python versions.
