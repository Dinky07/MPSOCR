"""python3 -m unittest discover -s tests   (no GPU or Ollama needed)"""
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import model_call                                   # noqa: E402
from tests.fake_ollama import FakeOllama            # noqa: E402


class ModelCallTest(unittest.TestCase):
    def test_chat_sends_body_unchanged(self):
        body = {"model": "m", "stream": False, "format": "json", "messages": [{"role": "user", "content": "hi"}]}
        with FakeOllama({"message": {"content": '{"a": 1}'}}) as o:
            self.assertEqual(model_call.chat_content(o.url, body, 5), '{"a": 1}')
            self.assertEqual(o.requests, [("POST", "/api/chat", body)])

    def test_generate(self):
        with FakeOllama({"response": "x"}) as o:
            self.assertEqual(model_call.generate(o.url, {"model": "m"}, 5)["response"], "x")
            self.assertEqual(o.requests[0][1], "/api/generate")

    def test_loaded_version_up_unload(self):
        with FakeOllama({"models": [{"name": "qwen"}], "version": "0.9"}) as o:
            self.assertEqual([m["name"] for m in model_call.loaded(o.url)], ["qwen"])
            self.assertEqual(model_call.version(o.url), "0.9")
            self.assertTrue(model_call.is_up(o.url))
            model_call.unload(o.url, "qwen")
            self.assertEqual(o.requests[-1], ("POST", "/api/generate", {"model": "qwen", "keep_alive": 0}))

    def test_down(self):
        self.assertFalse(model_call.is_up("http://127.0.0.1:9", timeout=1))
        with self.assertRaises(urllib.error.URLError):
            model_call.loaded("http://127.0.0.1:9", timeout=1)

    def test_http_error_reaches_caller(self):
        with FakeOllama({"error": "bad"}, status=400) as o:
            with self.assertRaises(urllib.error.HTTPError) as e:
                model_call.chat(o.url, {}, 5)
            self.assertEqual(e.exception.code, 400)

    def test_check_length(self):
        model_call.check_length({"done_reason": "stop"}, 10)
        with self.assertRaisesRegex(RuntimeError, "10-token cap"):
            model_call.check_length({"done_reason": "length", "eval_duration": 3e9}, 10)

    def test_image_b64(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"abc")
        try:
            self.assertEqual(model_call.image_b64(f.name), "YWJj")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
