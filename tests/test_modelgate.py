import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pathlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import modelgate as mg  # noqa: E402

KEY = "test-key"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if not FIXTURES.is_dir():  # fallback: repo vecino docpipe en el entorno de desarrollo
    FIXTURES = Path.home() / "Repos/docpipe/tests/fixtures"


class Stub(BaseHTTPRequestHandler):
    mode = "ok"           # ok | bad_json | broken | refusal_once
    calls = 0
    seen_prompts = []
    last_payload = {}

    def log_message(self, *args):
        pass

    def _ok(self):
        return self.headers.get("Authorization") == f"Bearer {KEY}"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._ok():
            self._send(401, {"error": "unauthorized"})
            return
        self._send(200, {"data": [{"id": "stub-model", "owned_by": "test"}]})

    def do_POST(self):
        if not self._ok():
            self._send(401, {"error": "unauthorized"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        Stub.last_payload = payload
        content = "eco:" + payload["messages"][-1]["content"][:20]
        Stub.calls += 1
        if Stub.mode == "tool_once" and not any(m.get("role") == "tool" for m in payload["messages"]):
            self._send(200, {"choices": [{"message": {
                "role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "web_search", "arguments": '{"query": "algo"}'}}]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5}})
            return
        ultimo = payload["messages"][-1].get("content", "")
        if Stub.mode == "refusal_once" and "ya está en tu contexto" not in ultimo:
            # determinista: niega el acceso hasta que el runtime reintenta
            content = "Lo siento, pero no tengo acceso a tus archivos locales."
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in [content[:5], content[5:]]:
                chunk = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            done = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 4}}
            self.wfile.write(b"data: " + json.dumps(done).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._send(200, {
            "model": payload.get("model"),
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5},
        })


def http_profile(port, sensitivity="cloud-ok"):
    return {
        "kind": "openai",
        "transport": "http",
        "base_url": f"http://127.0.0.1:{port}/v1",
        "model": "stub-model",
        "sensitivity": sensitivity,
        "api_key": {"source": "env", "var": "STUB_KEY"},
    }


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        os.environ["MODELGATE_STATE"] = self.tmp.name
        os.environ["STUB_KEY"] = KEY
        # el stub es estado de clase: reiniciarlo en cada test o se contagia
        Stub.mode = "ok"
        Stub.calls = 0
        Stub.seen_prompts = []
        Stub.last_payload = {}


class TestConfig(Base):
    def test_missing_file(self):
        with self.assertRaises(mg.CfgError):
            mg.load_config(Path(self.tmp.name) / "no-existe.json")

    def test_example_is_valid(self):
        cfg = mg.load_config(ROOT / "models.json.example")
        self.assertIn("local", cfg["profiles"])

    def test_unknown_kind(self):
        with self.assertRaises(mg.CfgError):
            mg.validate_profile("x", {"kind": "magia"})

    def test_http_needs_base_url(self):
        with self.assertRaises(mg.CfgError):
            mg.validate_profile("x", {"kind": "openai", "transport": "http", "model": "m"})

    def test_default_profile_must_exist(self):
        path = Path(self.tmp.name) / "c.json"
        path.write_text(json.dumps({"default_profile": "nube", "profiles": {
            "local": {"kind": "hermes", "model": "x", "sensitivity": "local-only"}}}))
        with self.assertRaises(mg.CfgError):
            mg.load_config(path)


class TestPolicy(Base):
    def test_local_only_blocked_on_cloud_profile(self):
        with self.assertRaises(mg.PolicyError):
            mg.check_policy("cloud", {"sensitivity": "cloud-ok"}, "local-only")

    def test_local_only_allowed_on_local_profile(self):
        mg.check_policy("local", {"sensitivity": "local-only"}, "local-only")

    def test_cloud_ok_allowed_anywhere(self):
        mg.check_policy("local", {"sensitivity": "local-only"}, "cloud-ok")

    def test_bad_sensitivity(self):
        with self.assertRaises(mg.CfgError):
            mg.check_policy("local", {"sensitivity": "local-only"}, "secreto")

    def test_route_never_falls_back_to_cloud(self):
        cfg = {"default_profile": "cloud", "profiles": {
            "cloud": {"kind": "hermes", "sensitivity": "cloud-ok"},
            "local": {"kind": "openai", "transport": "http", "base_url": "http://x/v1",
                      "model": "m", "sensitivity": "local-only"}}}
        name, prof = mg.route_profile(cfg, "local-only")
        self.assertEqual(name, "local")
        self.assertEqual(prof["sensitivity"], "local-only")

    def test_route_without_local_profile_raises(self):
        cfg = {"default_profile": "cloud", "profiles": {
            "cloud": {"kind": "hermes", "sensitivity": "cloud-ok"}}}
        with self.assertRaises(mg.PolicyError):
            mg.route_profile(cfg, "local-only")


class TestRun(Base):
    def test_run_returns_content_and_usage(self):
        res = mg.run_profile("stub", http_profile(self.port), "hola mundo", max_tokens=16)
        self.assertTrue(res["content"].startswith("eco:hola mundo"))
        self.assertEqual(res["usage"]["completion_tokens"], 5)
        self.assertGreaterEqual(res["ms"], 0)

    def test_wrong_key_is_unreachable(self):
        os.environ["STUB_KEY"] = "clave-mala"
        with self.assertRaises(mg.Unreachable):
            mg.run_profile("stub", http_profile(self.port), "hola")

    def test_system_message_included(self):
        res = mg.run_profile("stub", http_profile(self.port), "abc", system="sé breve")
        self.assertIn("eco:abc", res["content"])

    def test_audit_line_and_permissions(self):
        mg.audit({"ts": "2026-01-01T00:00:00+0100", "profile": "stub", "prompt_sha256": "x"})
        path = Path(self.tmp.name) / "audit.jsonl"
        self.assertTrue(path.exists())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("stub", path.read_text())


class TestCli(Base):
    def _cfg(self, sensitivity_cloud="cloud-ok"):
        path = Path(self.tmp.name) / "models.json"
        path.write_text(json.dumps({
            "default_profile": "local",
            "profiles": {
                "local": http_profile(self.port, "local-only"),
                "cloud": {"kind": "hermes", "provider": "p", "model": "m",
                          "sensitivity": sensitivity_cloud},
            }}))
        return path

    def _run(self, *args, cfg_path=None):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(cfg_path or self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        return subprocess.run([sys.executable, str(ROOT / "modelgate.py"), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_list_json(self):
        out = self._run("list", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = json.loads(out.stdout)
        self.assertEqual({r["profile"] for r in rows}, {"local", "cloud"})

    def test_run_through_cli(self):
        out = self._run("run", "local", "--prompt", "prueba cli", "--sensitivity", "local-only")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("eco:prueba cli", out.stdout)

    def test_policy_exit_code(self):
        out = self._run("run", "cloud", "--prompt", "dato sensible", "--sensitivity", "local-only")
        self.assertEqual(out.returncode, mg.EXIT_POLICY)
        self.assertIn("política", out.stderr)

    def test_config_exit_code(self):
        out = self._run("list", cfg_path=Path(self.tmp.name) / "ausente.json")
        self.assertEqual(out.returncode, mg.EXIT_CONFIG)

    def test_status_reports_reachable(self):
        out = self._run("status", "local", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)["local"]["reachable"])


class TestDocs(Base):
    """El subcomando docs: política, transporte de flags y clave por entorno."""

    def _fake_docpipe(self):
        script = self.home / "fakepipe.py"
        script.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:],"
            " 'key': bool(os.environ.get('DOCPIPE_API_KEY'))}))\n")
        return script

    def _cfg(self, sensitivity="local-only"):
        path = self.home / "models.json"
        path.write_text(json.dumps({
            "default_profile": "local",
            "profiles": {
                "local": http_profile(self.port, sensitivity),
                "cloud": {"kind": "hermes", "model": "m", "sensitivity": "cloud-ok"},
            }}))
        return path

    def _run(self, *args, script=None):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["DOCPIPE"] = str(script or self._fake_docpipe())
        env["STUB_KEY"] = KEY
        return subprocess.run([sys.executable, str(ROOT / "modelgate.py"), "docs", *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_docs_pasa_flags_y_clave(self):
        out = self._run(str(FIXTURES), "--kind", "factura", "--dry-run", "--limit", "3")
        self.assertEqual(out.returncode, 0, out.stderr)
        payload = json.loads(out.stdout.strip().splitlines()[-1])
        argv = payload["argv"]
        self.assertIn("--kind", argv)
        self.assertEqual(argv[argv.index("--kind") + 1], "factura")
        self.assertIn("--dry-run", argv)
        self.assertEqual(argv[argv.index("--limit") + 1], "3")
        self.assertTrue(payload["key"], "la clave debe ir por entorno, no por argv")
        self.assertNotIn(KEY, " ".join(argv))

    def test_docs_bloquea_perfil_cloud(self):
        out = self._run(str(FIXTURES), "--profile", "cloud", "--dry-run")
        self.assertEqual(out.returncode, mg.EXIT_POLICY)
        self.assertIn("política", out.stderr)

    def test_docs_sin_docpipe(self):
        out = self._run(str(FIXTURES), "--dry-run", script=self.home / "no-existe.py")
        self.assertEqual(out.returncode, mg.EXIT_CONFIG)
        self.assertIn("docpipe", out.stderr)


class TestChat(Base):
    def _fake_docpipe(self):
        """Stub de docpipe: emite una linea JSONL como `docpipe extract`."""
        script = self.home / "fakedocpipe.py"
        script.write_text(
            "import json, sys, pathlib\n"
            "p = sys.argv[sys.argv.index('--path') + 1] if '--path' in sys.argv else 'doc'\n"
            "t = 'contenido de ' + pathlib.Path(p).name\n"
            "print(json.dumps({'status': 'ok', 'chars': len(t), 'text': t}))\n")
        return script

    def _cfg(self):
        path = self.home / "models.json"
        path.write_text(json.dumps({
            "default_profile": "local",
            "profiles": {
                "local": http_profile(self.port, "local-only"),
                "cloud": {"kind": "hermes", "model": "m", "sensitivity": "cloud-ok"},
            }}))
        return path

    def _run(self, *args):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        return subprocess.run([sys.executable, str(ROOT / "modelgate.py"), "chat", *args],
                              capture_output=True, text=True, env=env, timeout=60,
                              stdin=subprocess.DEVNULL)

    def test_turno_unico_con_streaming(self):
        out = self._run("--prompt", "hola mundo", "--max-tokens", "32")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("eco:hola mundo", out.stdout)
        self.assertIn("tok]", out.stdout)   # usage llegó por stream_options

    def test_repl_acepta_stdin_y_guarda(self):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        transcript = self.home / "chat.txt"
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--max-tokens", "32"],
            input=f"primera pregunta\n/guardar {transcript}\n/salir\n",
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("eco:primera pregunta", out.stdout)
        self.assertTrue(transcript.exists())
        self.assertIn("primera pregunta", transcript.read_text())

    def test_chat_respeta_politica(self):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--profile", "cloud",
             "--sensitivity", "local-only", "--prompt", "dato sensible"],
            capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, mg.EXIT_POLICY)
        self.assertIn("política", out.stderr)

    def test_chat_context_carga_documentos(self):
        fake = self.home / "fakepipe_ctx.py"
        fake.write_text(
            "import json\n"
            "print(json.dumps({'path': '/x/factura.pdf', 'name': 'factura.pdf',"
            " 'status': 'ok', 'chars': 20, 'text': 'TOTAL 290,40 EUR CIF B12345678'}))\n"
            "print(json.dumps({'path': '/x/foto.png', 'name': 'foto.png',"
            " 'status': 'pendiente_ocr', 'chars': 0}))\n")
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        env["DOCPIPE"] = str(fake)
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--context", str(self.home),
             "--prompt", "¿cuál es el total?"],
            capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("contexto: 1 documentos", out.stdout)
        system = Stub.last_payload["messages"][0]["content"]
        self.assertIn("factura.pdf", system)
        self.assertIn("290,40 EUR", system)
        self.assertNotIn("foto.png", system)      # pendiente_ocr no se inyecta

    def test_contexto_declara_la_regla(self):
        fake = self.home / "fp.py"
        fake.write_text(
            "import json\n"
            "print(json.dumps({'path': '/x/a.pdf', 'name': 'a.pdf', 'status': 'ok',"
            " 'chars': 5, 'text': 'TOTAL 10 EUR'}))\n")
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        env["DOCPIPE"] = str(fake)
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--context", str(self.home),
             "--prompt", "resume"],
            capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        system = Stub.last_payload["messages"][0]["content"]
        self.assertIn("REGLA OBLIGATORIA", system)
        self.assertIn("Ficheros incluidos: a.pdf", system)

    def test_reintenta_si_niega_el_acceso(self):
        Stub.mode = "refusal_once"
        fake = self.home / "fp2.py"
        fake.write_text(
            "import json\n"
            "print(json.dumps({'path': '/x/a.pdf', 'name': 'a.pdf', 'status': 'ok',"
            " 'chars': 5, 'text': 'TOTAL 10 EUR'}))\n")
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        env["DOCPIPE"] = str(fake)
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--context", str(self.home),
             "--prompt", "analiza los documentos de /ruta y dime el total"],
            capture_output=True, text=True, env=env, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[reintento", out.stdout)
        self.assertEqual(Stub.calls, 2)
        self.assertNotIn("no tengo acceso", out.stdout.split("[reintento")[-1])

    def test_la_busqueda_entrega_los_resultados_ya_leidos(self):
        # El modelo pequeno ignora las URLs: la busqueda debe devolver el texto.
        viejo_search, viejo_fetch = mg.web_search, mg.fetch_page
        mg.web_search = lambda q, n=5: [{"title": "Tarifas", "url": "https://ejemplo.es/tarifas"},
                                        {"title": "Otra", "url": "https://ejemplo.es/otra"}]
        mg.fetch_page = lambda url, cap=6000: "Punta 0,249 euros/kWh en " + url
        try:
            out = json.loads(mg._run_tool({"function": {"name": "web_search",
                                                        "arguments": '{"query": "tarifas"}'}}))
            self.assertEqual(len(out["ya_leidos"]), 2)
            self.assertIn("0,249", out["ya_leidos"][0]["texto"])
            self.assertEqual(out["ya_leidos"][0]["url"], "https://ejemplo.es/tarifas")
            self.assertEqual(mg._ultimas_urls(), ["https://ejemplo.es/tarifas", "https://ejemplo.es/otra"])
        finally:
            mg.web_search, mg.fetch_page = viejo_search, viejo_fetch

    def test_fetch_source_lee_la_url_declarada(self):
        doc = self.home / "precios.txt"
        doc.write_text("Punta 0,249 euros/kWh")
        f = self.home / "fuentes.json"
        f.write_text(json.dumps({"tarifa": {"url": doc.as_uri(), "desc": "de prueba"}}))
        viejo = mg.FUENTES_PATH
        mg.FUENTES_PATH = str(f)
        try:
            out = mg._run_tool({"function": {"name": "fetch_source",
                                             "arguments": '{"name": "tarifa"}'}})
            self.assertIn("0,249", out)
            err = mg._run_tool({"function": {"name": "fetch_source",
                                             "arguments": '{"name": "inexistente"}'}})
            self.assertIn("no declarada", err)
            self.assertIn("tarifa", err)
        finally:
            mg.FUENTES_PATH = viejo

    def test_docs_y_leer_en_la_sesion(self):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        env["DOCPIPE"] = str(self._fake_docpipe())
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat",
             "--root", str(FIXTURES), "--prompt", ""],
            input="/docs\n/leer factura.txt\n/salir\n",
            capture_output=True, text=True, env=env, timeout=120)
        self.assertIn("factura.txt", out.stdout)
        self.assertIn("cargado factura.txt", out.stdout)

    def test_leer_varios_y_sin_duplicar(self):
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        env["DOCPIPE"] = str(self._fake_docpipe())
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat",
             "--root", str(FIXTURES), "--prompt", ""],
            input="/leer factura.txt correo.eml\n/leer correo.eml\n/salir\n",
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("cargado factura.txt", out.stdout)
        self.assertIn("cargado correo.eml", out.stdout)
        self.assertIn("ya estaba cargado correo.eml", out.stdout)

    def test_leer_reintenta_si_el_modelo_niega(self):
        Stub.mode = "refusal_once"
        (self.home / "a.pdf").write_bytes(b"%PDF-1.4 texto de prueba")
        fake = self.home / "fp3.py"
        fake.write_text(
            "import json\n"
            "print(json.dumps({'path': '/x/a.pdf', 'name': 'a.pdf', 'status': 'ok',"
            " 'chars': 5, 'text': 'TOTAL 10 EUR'}))\n")
        env = dict(os.environ)
        env["MODELGATE_CONFIG"] = str(self._cfg())
        env["MODELGATE_STATE"] = self.tmp.name
        env["STUB_KEY"] = KEY
        env["DOCPIPE"] = str(fake)
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat",
             "--root", str(self.home), "--prompt", ""],
            input="/leer a.pdf\nanaliza los documentos y dime el total\n/salir\n",
            capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("[reintento", out.stdout)
        self.assertNotIn("no tengo acceso", out.stdout.split("[reintento")[-1])

    def test_sin_contexto_no_reintenta(self):
        Stub.mode = "refusal_once"
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--prompt", "hola"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            env={**os.environ, "MODELGATE_CONFIG": str(self._cfg()),
                 "MODELGATE_STATE": self.tmp.name, "STUB_KEY": KEY})
        self.assertNotIn("[reintento", out.stdout)


class TestWebTools(Base):
    def test_unwrap_ddg_saca_la_url_real(self):
        wrapped = ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.somenergia.coop%2Fes%2Ftarifas&rut=abc")
        self.assertEqual(mg._unwrap_ddg(wrapped), "https://www.somenergia.coop/es/tarifas")

    def test_unwrap_ddg_descarta_anuncios(self):
        ad = ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fduckduckgo.com%2Fy.js%3Fad_domain%3Dx")
        self.assertEqual(mg._unwrap_ddg(ad), "")

    def test_unwrap_ddg_pasa_urls_directas(self):
        self.assertEqual(mg._unwrap_ddg("https://ejemplo.com/a"), "https://ejemplo.com/a")

    def test_bucle_ejecuta_la_herramienta_y_responde(self):
        Stub.mode = "tool_once"
        original = mg._run_tool
        visto = {}

        def falso(call, *a, **k):
            visto["name"] = call["function"]["name"]
            return '{"results": [{"title": "x", "url": "https://x"}]}'

        mg._run_tool = falso
        try:
            texto, usage = mg.chat_with_tools(
                f"http://127.0.0.1:{self.port}/v1", "stub-model",
                [{"role": "user", "content": "busca algo"}], KEY, 64, 30)
        finally:
            mg._run_tool = original
        self.assertEqual(visto["name"], "web_search")
        self.assertIn("eco:", texto)
        self.assertTrue(any(m.get("role") == "tool" for m in Stub.last_payload["messages"]))
        self.assertGreater(usage.get("prompt_tokens", 0), 0)

    def test_sin_tools_no_manda_esquema(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "modelgate.py"), "chat", "--prompt", "hola",
             "--max-tokens", "32"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
            env={**os.environ, "MODELGATE_CONFIG": str(self._cfg_docs_local()),
                 "MODELGATE_STATE": self.tmp.name, "STUB_KEY": KEY})
        self.assertNotIn("tools", Stub.last_payload)

    def _cfg_docs_local(self):
        path = self.home / "m.json"
        path.write_text(json.dumps({"default_profile": "local", "profiles": {
            "local": http_profile(self.port, "local-only")}}))
        return path


class TestLifecycle(Base):
    """Política de apagado: pura, sin red."""

    def test_if_woken_solo_si_lo_despertamos(self):
        prof = {"lifecycle": {"shutdown": "if_woken"}}
        self.assertTrue(mg.shutdown_policy(prof, True))
        self.assertFalse(mg.shutdown_policy(prof, False))

    def test_never_y_always(self):
        self.assertFalse(mg.shutdown_policy({"lifecycle": {"shutdown": "never"}}, True))
        self.assertTrue(mg.shutdown_policy({"lifecycle": {"shutdown": "always"}}, False))

    def test_por_defecto_es_if_woken(self):
        self.assertTrue(mg.shutdown_policy({}, True))
        self.assertFalse(mg.shutdown_policy({}, False))

    def test_auto_stop_y_conf(self):
        self.assertTrue(mg.lifecycle_auto({"lifecycle": {"auto_stop": True}}))
        self.assertFalse(mg.lifecycle_auto({"lifecycle": {}}))
        self.assertEqual(mg.lifecycle_conf({"lifecycle": {"task": "x"}}), {"task": "x"})
        self.assertEqual(mg.lifecycle_conf({}), {})



if __name__ == "__main__":
    unittest.main(verbosity=2)
