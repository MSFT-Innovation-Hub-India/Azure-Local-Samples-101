import io
import json
import ssl
import subprocess
import unittest
import urllib.error
from unittest.mock import patch

import chat


class ChatTests(unittest.TestCase):
    def test_load_configuration(self):
        with patch("chat.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="dGVzdC1rZXk=\n"),
                subprocess.CompletedProcess([], 0, stdout="qwen-model\n"),
            ]
            self.assertEqual(chat.load_configuration(), ("test-key", "qwen-model"))
            self.assertIn("jsonpath={.data.primary-key}", run.call_args_list[0].args[0])
            self.assertIn(
                "jsonpath={.status.resolvedModel.name}", run.call_args_list[1].args[0]
            )

    def test_invalid_key(self):
        for value in ("not-base64!", "/w==", "IA=="):
            with self.subTest(value=value), patch("chat.kubectl_value", return_value=value):
                with self.assertRaises(RuntimeError):
                    chat.load_configuration()

    def test_kubectl_errors(self):
        errors = [
            FileNotFoundError(),
            subprocess.CalledProcessError(1, ["kubectl"]),
            subprocess.TimeoutExpired(["kubectl"], 30),
        ]
        for error in errors:
            with self.subTest(error=error), patch("chat.subprocess.run", side_effect=error):
                with self.assertRaises(RuntimeError):
                    chat.kubectl_value("secret", "test", "{.data.primary-key}")
        with patch("chat.subprocess.run") as run:
            run.return_value.stdout = " "
            with self.assertRaisesRegex(RuntimeError, "empty value"):
                chat.kubectl_value("secret", "test", "{.data.primary-key}")

    def test_completion_request(self):
        messages = [chat.SYSTEM_MESSAGE, {"role": "user", "content": "Hello"}]
        context = ssl.create_default_context()
        with patch("chat.urllib.request.urlopen") as open_url:
            open_url.return_value.__enter__.return_value = io.BytesIO(
                b'{"choices":[{"message":{"content":"Hello back"}}]}'
            )
            self.assertEqual(
                chat.complete("https://localhost:5000/v1/chat/completions",
                              "test-key", "qwen", messages, 50, 180, context),
                "Hello back",
            )
            request = open_url.call_args.args[0]
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.get_header("Api-key"), "test-key")
            self.assertEqual(json.loads(request.data), {
                "model": "qwen", "messages": messages, "max_tokens": 50,
            })
            self.assertIs(open_url.call_args.kwargs["context"], context)
            self.assertEqual(open_url.call_args.kwargs["timeout"], 180)

    def test_bad_responses(self):
        for body in (b"not json", b"{}", b"null", b'{"choices":[]}',
                     b'{"choices":[{"message":{"content":null}}]}',
                     b'{"choices":[{"message":{"content":""}}]}'):
            with self.subTest(body=body), patch("chat.urllib.request.urlopen") as open_url:
                open_url.return_value.__enter__.return_value = io.BytesIO(body)
                with self.assertRaises(RuntimeError):
                    chat.complete("https://localhost", "key", "qwen", [], 50, 1,
                                  ssl.create_default_context())

    def test_network_errors(self):
        errors = [
            urllib.error.HTTPError("https://localhost", 401, "Unauthorized", {}, None),
            urllib.error.URLError("Connection refused"),
            TimeoutError(),
        ]
        for error in errors:
            with self.subTest(error=error), patch(
                "chat.urllib.request.urlopen", side_effect=error
            ):
                with self.assertRaises(RuntimeError):
                    chat.complete("https://localhost", "key", "qwen", [], 50, 1,
                                  ssl.create_default_context())

    def test_loop_history_reset_and_failed_turn(self):
        with (
            patch("sys.argv", ["chat.py", "--insecure"]),
            patch("chat.load_configuration", return_value=("key", "qwen")),
            patch("builtins.input", side_effect=[
                "", "First", "Follow-up", "Failed", "Retry", "/reset", "New", "/exit",
            ]),
            patch("chat.complete", side_effect=[
                "One", "Two", RuntimeError("HTTP 500"), "Three", "Fresh",
            ]) as complete,
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(chat.main(), 0)
            calls = complete.call_args_list
            self.assertEqual([m["content"] for m in calls[1].args[3]][1:],
                             ["First", "One", "Follow-up"])
            self.assertEqual([m["content"] for m in calls[3].args[3]][1:],
                             ["First", "One", "Follow-up", "Two", "Retry"])
            self.assertEqual(calls[4].args[3],
                             [chat.SYSTEM_MESSAGE, {"role": "user", "content": "New"}])
            self.assertEqual(calls[0].args[6].verify_mode, ssl.CERT_NONE)
            self.assertIn("This turn was not saved", stderr.getvalue())

    def test_secure_default_and_eof(self):
        with (
            patch("sys.argv", ["chat.py"]),
            patch("chat.load_configuration", return_value=("key", "qwen")),
            patch("builtins.input", side_effect=["Hello", EOFError()]),
            patch("chat.complete", return_value="Hi") as complete,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(chat.main(), 0)
            context = complete.call_args.args[6]
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_loop_sends_history_in_http_payload(self):
        response = b'{"choices":[{"message":{"content":"OK"}}]}'
        with (
            patch("sys.argv", ["chat.py", "--debug"]),
            patch("chat.load_configuration", return_value=("private-key", "qwen")),
            patch("builtins.input", side_effect=[
                "/history", "My favorite fruit is mango.", "/history",
                "What is my favorite fruit?", "/reset", "/history", "/exit",
            ]),
            patch("chat.urllib.request.urlopen") as open_url,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            open_url.return_value.__enter__.side_effect = [
                io.BytesIO(response), io.BytesIO(response),
            ]
            self.assertEqual(chat.main(), 0)
            self.assertEqual(open_url.call_count, 2)
            first = json.loads(open_url.call_args_list[0].args[0].data)
            second = json.loads(open_url.call_args_list[1].args[0].data)
            self.assertEqual(first["messages"], [
                chat.SYSTEM_MESSAGE,
                {"role": "user", "content": "My favorite fruit is mango."},
            ])
            self.assertEqual(second["messages"], first["messages"] + [
                {"role": "assistant", "content": "OK"},
                {"role": "user", "content": "What is my favorite fruit?"},
            ])
            self.assertIn("User: My favorite fruit is mango.", stdout.getvalue())
            self.assertIn("Assistant: OK", stdout.getvalue())
            self.assertEqual(stdout.getvalue().count("No conversation turns saved yet."), 2)
            self.assertIn("Sending 2 messages: system, user", stderr.getvalue())
            self.assertIn(
                "Sending 4 messages: system, user, assistant, user", stderr.getvalue()
            )
            self.assertNotIn("private-key", stdout.getvalue() + stderr.getvalue())
            self.assertNotIn("mango", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
