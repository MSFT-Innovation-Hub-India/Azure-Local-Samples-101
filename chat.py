"""Interactive chat with the Qwen model through a Kubernetes port-forward."""

import argparse
import base64
import binascii
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request


NAMESPACE = "foundry-local-operator"
DEPLOYMENT = "qwen2-5-0-5b-cpu"
SYSTEM_MESSAGE = {"role": "system", "content": "You are a helpful assistant."}


def kubectl_value(resource: str, name: str, jsonpath: str) -> str:
    try:
        result = subprocess.run(
            [
                "kubectl", "get", resource, name,
                "-n", NAMESPACE, "-o", f"jsonpath={jsonpath}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("kubectl was not found. Install it and add it to PATH.") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not read {resource} {name}. Check your kubectl context, "
            "Arc proxy, permissions, and model deployment."
        ) from exc
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"{resource} {name} returned an empty value for {jsonpath}.")
    return value


def load_configuration() -> tuple[str, str]:
    encoded_key = kubectl_value(
        "secret", f"{DEPLOYMENT}-api-keys", "{.data.primary-key}"
    )
    try:
        api_key = base64.b64decode(encoded_key, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise RuntimeError("The Secret's primary-key is not valid base64 UTF-8.") from exc
    if not api_key.strip():
        raise RuntimeError("The Secret's primary-key is empty.")
    model = kubectl_value(
        "modeldeployment", DEPLOYMENT, "{.status.resolvedModel.name}"
    )
    return api_key, model


def complete(
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float,
    context: ssl.SSLContext,
) -> str:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Inference returned HTTP {exc.code}. Check the API key, model status, "
            "and conversation length; /reset clears the history."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            "Cannot reach the inference endpoint. Check the port-forward and model "
            "status. For a trusted local self-signed endpoint, use --insecure. "
            "If inference is slow, increase --timeout."
        ) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("The endpoint returned invalid JSON.") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("The endpoint returned no assistant message.") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("The endpoint returned an empty or non-text assistant message.")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint", default="https://localhost:5000/v1/chat/completions"
    )
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument(
        "--debug", action="store_true",
        help="Show outgoing message counts and roles, without content or API keys.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Disable TLS verification for a trusted local self-signed endpoint only.",
    )
    args = parser.parse_args()
    if args.max_tokens <= 0 or not 0 < args.timeout < float("inf"):
        parser.error("--max-tokens and --timeout must be positive finite values.")

    context = ssl.create_default_context()
    if args.insecure:
        print(
            "Warning: TLS certificate verification is disabled. "
            "Use only with your trusted local port-forward.",
            file=sys.stderr,
        )
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        api_key, model = load_configuration()
        print(
            f"Model: {model}\nCommands: /history shows history; "
            "/reset clears history; /exit quits.\n"
            "History is kept only until this process exits."
        )
        messages = [SYSTEM_MESSAGE.copy()]
        while True:
            question = input("\nYou: ").strip()
            if question.lower() in {"/exit", "/quit"}:
                return 0
            if question.lower() == "/reset":
                messages = [SYSTEM_MESSAGE.copy()]
                print("Conversation cleared.")
                continue
            if question.lower() == "/history":
                if len(messages) == 1:
                    print("No conversation turns saved yet.")
                for message in messages[1:]:
                    print(f"\n{message['role'].capitalize()}: {message['content']}")
                continue
            if not question:
                continue
            pending = messages + [{"role": "user", "content": question}]
            if args.debug:
                roles = ", ".join(message["role"] for message in pending)
                print(
                    f"Sending {len(pending)} messages: {roles}",
                    file=sys.stderr,
                )
            try:
                answer = complete(
                    args.endpoint, api_key, model, pending,
                    args.max_tokens, args.timeout, context,
                )
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                print("This turn was not saved. You can try again.", file=sys.stderr)
                continue
            messages = pending + [{"role": "assistant", "content": answer}]
            print(f"\nAssistant: {answer}")
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye.")
        return 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
