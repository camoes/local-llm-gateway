#!/usr/bin/env python3
"""modelgate — enruta trabajo entre un modelo local (llama.cpp) y uno cloud.

Regla dura: el trabajo marcado `local-only` solo puede ir a un perfil marcado
`local-only`. El transporte por defecto es un túnel SSH a 127.0.0.1, de modo que
el endpoint del modelo nunca se publica en la red local.

    modelgate list
    modelgate status
    modelgate tunnel up|down|status local
    modelgate run local --prompt "..." --sensitivity local-only
    modelgate route --sensitivity local-only --file factura.txt
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import socket
import subprocess
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_POLICY = 3
EXIT_UNREACHABLE = 4

SENSITIVITIES = ("cloud-ok", "local-only")


# ----------------------------------------------------------------- excepciones
class CfgError(Exception):
    """Configuración inválida o ausente."""


class PolicyError(Exception):
    """El destino no admite el nivel de sensibilidad del trabajo."""


class Unreachable(Exception):
    """El destino no responde."""


# ---------------------------------------------------------------------- estado
def state_dir() -> Path:
    d = Path(os.getenv("MODELGATE_STATE") or (Path.home() / ".modelgate"))
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def config_path() -> Path:
    return Path(os.getenv("MODELGATE_CONFIG") or Path(__file__).with_name("models.json"))


def audit(record: dict) -> None:
    """Una línea JSON por ejecución. Sin contenido del prompt, solo su hash."""
    path = state_dir() / "audit.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------- config
def load_config(path: Path | str | None = None) -> dict:
    p = Path(path) if path else config_path()
    if not p.exists():
        raise CfgError(f"falta {p}\n  cp {p.with_suffix('.json.example')} {p} y ajusta los datos")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CfgError(f"{p}: JSON inválido ({exc})")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise CfgError(f"{p}: falta el objeto 'profiles'")
    for name, prof in profiles.items():
        validate_profile(name, prof)
    default = data.get("default_profile")
    if default is not None and default not in profiles:
        raise CfgError(f"{p}: default_profile {default!r} no existe")
    return data


def validate_profile(name: str, prof: dict) -> None:
    if not isinstance(prof, dict):
        raise CfgError(f"{name}: debe ser un objeto")
    kind = prof.get("kind", "openai")
    if kind not in ("openai", "hermes"):
        raise CfgError(f"{name}: kind desconocido {kind!r} (openai|hermes)")
    sens = prof.get("sensitivity", "cloud-ok")
    if sens not in SENSITIVITIES:
        raise CfgError(f"{name}: sensitivity {sens!r} (cloud-ok|local-only)")
    if kind == "openai":
        transport = prof.get("transport", "http")
        if transport not in ("http", "ssh-tunnel"):
            raise CfgError(f"{name}: transport {transport!r} (http|ssh-tunnel)")
        if transport == "http" and not prof.get("base_url"):
            raise CfgError(f"{name}: transport http necesita base_url")
        if transport == "ssh-tunnel" and not (prof.get("ssh_host") or prof.get("host")):
            raise CfgError(f"{name}: transport ssh-tunnel necesita ssh_host")
        if not prof.get("model"):
            raise CfgError(f"{name}: falta model")
    key = prof.get("api_key")
    if key is not None:
        if not isinstance(key, dict):
            raise CfgError(f"{name}: api_key debe ser un objeto")
        src = key.get("source", "none")
        if src not in ("none", "env", "ssh-file"):
            raise CfgError(f"{name}: api_key.source {src!r} (none|env|ssh-file)")
        if src == "env" and not key.get("var"):
            raise CfgError(f"{name}: api_key.source env necesita 'var'")
        if src == "ssh-file" and not key.get("path"):
            raise CfgError(f"{name}: api_key.source ssh-file necesita 'path'")


def pick_profile(cfg: dict, name: str | None) -> tuple[str, dict]:
    profiles = cfg["profiles"]
    if name:
        if name not in profiles:
            raise CfgError(f"perfil desconocido: {name}. Disponibles: {', '.join(sorted(profiles))}")
        return name, profiles[name]
    default = cfg.get("default_profile")
    if not default:
        raise CfgError("indica un perfil o define default_profile")
    return default, profiles[default]


def check_policy(name: str, prof: dict, sensitivity: str) -> None:
    """Un dato local-only no puede salir a un perfil que acepta la nube."""
    if sensitivity not in SENSITIVITIES:
        raise CfgError(f"sensibilidad {sensitivity!r} (cloud-ok|local-only)")
    if sensitivity == "local-only" and prof.get("sensitivity") != "local-only":
        raise PolicyError(
            f"el perfil '{name}' está marcado '{prof.get('sensitivity')}' y el trabajo es 'local-only'"
        )


# ------------------------------------------------------------------ net-conn
def netconn():
    """net-conn se referencia por ruta, no se copia (contrato del repo)."""
    base = Path(os.getenv("NETCONN") or (Path.home() / "Repos/net-conn"))
    if not (base / "remote.py").exists():
        raise CfgError(f"net-conn no encontrado en {base} (define NETCONN)")
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))
    import remote  # noqa: PLC0415

    return remote


def netconn_host(remote, name: str) -> dict:
    hosts = remote.load_hosts(Path(remote.DEFAULT_CONFIG))
    return remote.get_host(hosts, name)


# --------------------------------------------------------------------- túnel
def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _pidfile(name: str) -> Path:
    return state_dir() / f"tunnel-{name}.pid"


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def tunnel_status(prof: dict, name: str) -> dict:
    port = int(prof.get("local_port", 18080))
    pf = _pidfile(name)
    pid = int(pf.read_text().strip()) if pf.exists() else None
    return {"pid": pid, "alive": _alive(pid), "port_open": port_open("127.0.0.1", port), "local_port": port}


def tunnel_up(prof: dict, name: str, timeout: int = 30) -> dict:
    st = tunnel_status(prof, name)
    if st["alive"] and st["port_open"]:
        return st
    remote = netconn()
    ssh_name = prof.get("ssh_host") or prof.get("host")
    host = netconn_host(remote, ssh_name)
    lport = int(prof.get("local_port", 18080))
    rport = int(prof.get("remote_port", 8080))
    rhost = prof.get("remote_host", "127.0.0.1")
    log_path = state_dir() / f"tunnel-{name}.log"
    cmd = remote.ssh_base(host) + [
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-L", f"127.0.0.1:{lport}:{rhost}:{rport}",
    ]
    with open(log_path, "ab") as log:
        # stdin a DEVNULL: un `ssh -N` que hereda stdin se come la entrada del
        # proceso que lo lanzó (p. ej. el REPL de `modelgate chat`).
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True)
    _pidfile(name).write_text(str(proc.pid))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open("127.0.0.1", lport):
            return tunnel_status(prof, name)
        if proc.poll() is not None:
            raise Unreachable(f"ssh terminó con {proc.returncode}; ver {log_path}")
        time.sleep(0.3)
    proc.terminate()
    raise Unreachable(f"el túnel no abrió 127.0.0.1:{lport} en {timeout}s; ver {log_path}")


def tunnel_down(name: str) -> dict:
    pf = _pidfile(name)
    pid = int(pf.read_text().strip()) if pf.exists() else None
    if _alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        for _ in range(30):
            if not _alive(pid):
                break
            time.sleep(0.1)
    pf.unlink(missing_ok=True)
    return {"pid": pid, "alive": _alive(pid)}


# ---------------------------------------------------------------------- claves
def api_key(prof: dict) -> str | None:
    spec = prof.get("api_key") or {}
    src = spec.get("source", "none")
    if src == "none":
        return None
    if src == "env":
        value = os.getenv(spec["var"])
        if not value:
            raise CfgError(f"falta la variable de entorno {spec['var']}")
        return value
    remote = netconn()
    host = netconn_host(remote, spec.get("host") or prof.get("ssh_host") or prof.get("host"))
    path = spec["path"]
    if remote.is_windows(host):
        cmd = 'powershell -NoProfile -Command "Get-Content \'%s\' -Raw"' % path
    else:
        cmd = "cat '%s'" % path
    out, code = remote.ssh_out(host, cmd)
    if code != 0 or not out.strip():
        raise CfgError(f"no se pudo leer la clave en {host['host']}:{path} (código {code})")
    return out.strip()


# ---------------------------------------------------------------------- http
def http_json(url: str, payload: dict | None = None, key: str | None = None, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise Unreachable(f"HTTP {exc.code} en {url}: {body}")
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(f"sin conexión a {url}: {exc}")


def endpoint(prof: dict, name: str) -> str:
    transport = prof.get("transport", "http")
    if transport == "ssh-tunnel":
        tunnel_up(prof, name)
        return f"http://127.0.0.1:{int(prof.get('local_port', 18080))}/v1"
    return str(prof["base_url"]).rstrip("/")


# ------------------------------------------------------------------ ejecución
def run_profile(name: str, prof: dict, prompt: str, system: str | None = None,
                temperature: float | None = None, max_tokens: int | None = None,
                timeout: int = 300) -> dict:
    t0 = time.time()
    if prof.get("kind", "openai") == "hermes":
        return _run_hermes(name, prof, prompt, timeout, t0)

    defaults = prof.get("defaults") or {}
    system = system or defaults.get("system")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": prof["model"],
        "messages": messages,
        "temperature": temperature if temperature is not None else defaults.get("temperature", 0.1),
        "max_tokens": max_tokens if max_tokens is not None else defaults.get("max_tokens", 1024),
    }
    if defaults.get("extra_body"):
        payload.update(defaults["extra_body"])
    resp = http_json(endpoint(prof, name) + "/chat/completions", payload, api_key(prof), timeout)
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "profile": name,
        "model": resp.get("model", prof.get("model")),
        "content": message.get("content") or "",
        "reasoning": message.get("reasoning_content"),
        "finish_reason": choice.get("finish_reason"),
        "usage": resp.get("usage") or {},
        "ms": int((time.time() - t0) * 1000),
        "sensitivity": prof.get("sensitivity"),
    }


def _run_hermes(name: str, prof: dict, prompt: str, timeout: int, t0: float) -> dict:
    cmd = [os.getenv("MODELGATE_HERMES", "hermes"), "-z", prompt]
    if prof.get("provider"):
        cmd += ["--provider", prof["provider"]]
    if prof.get("model"):
        cmd += ["-m", prof["model"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise CfgError("el CLI 'hermes' no está en PATH")
    if proc.returncode != 0:
        raise Unreachable(f"hermes falló ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:200]}")
    return {
        "profile": name,
        "model": prof.get("model"),
        "content": proc.stdout.strip(),
        "reasoning": None,
        "finish_reason": "stop",
        "usage": {},
        "ms": int((time.time() - t0) * 1000),
        "sensitivity": prof.get("sensitivity"),
    }


def route_profile(cfg: dict, sensitivity: str) -> tuple[str, dict]:
    """Primer perfil que acepte la sensibilidad pedida; local-only nunca cae a la nube."""
    if sensitivity == "local-only":
        for name, prof in cfg["profiles"].items():
            if prof.get("sensitivity") == "local-only":
                return name, prof
        raise PolicyError("ningún perfil marcado 'local-only' disponible")
    return pick_profile(cfg, None)


# ----------------------------------------------------------------------- CLI
def read_prompt(args) -> str:
    if args.prompt:
        return args.prompt
    if args.file:
        return Path(args.file).read_text(encoding="utf-8", errors="replace")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise CfgError("falta el prompt: --prompt, --file o por stdin")


def print_result(res: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    usage = res.get("usage") or {}
    print(res["content"])
    if usage:
        print(f"\n[{res['profile']} · {res['model']} · {res['ms']} ms · "
              f"prompt {usage.get('prompt_tokens', '?')} · gen {usage.get('completion_tokens', '?')}]",
              file=sys.stderr)


def cmd_list(cfg: dict, as_json: bool) -> int:
    rows = []
    for name, prof in cfg["profiles"].items():
        rows.append({
            "profile": name,
            "kind": prof.get("kind", "openai"),
            "transport": prof.get("transport", "-") if prof.get("kind", "openai") == "openai" else "cli",
            "model": prof.get("model", "-"),
            "sensitivity": prof.get("sensitivity", "cloud-ok"),
        })
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for r in rows:
            print(f"{r['profile']:<10} {r['kind']:<7} {r['transport']:<11} {r['sensitivity']:<11} {r['model']}")
    return EXIT_OK


def cmd_status(cfg: dict, names: list[str] | None, as_json: bool) -> int:
    out = {}
    worst = EXIT_OK
    for name in (names or list(cfg["profiles"])):
        prof = cfg["profiles"].get(name)
        if prof is None:
            raise CfgError(f"perfil desconocido: {name}")
        entry = {"kind": prof.get("kind", "openai"), "reachable": False, "detail": ""}
        try:
            if prof.get("kind", "openai") == "hermes":
                import shutil

                entry["reachable"] = shutil.which(os.getenv("MODELGATE_HERMES", "hermes")) is not None
                entry["detail"] = "CLI hermes" if entry["reachable"] else "CLI hermes no encontrado"
            else:
                base = endpoint(prof, name)
                data = http_json(base + "/models", None, api_key(prof), 15)
                models = [m.get("id") for m in (data.get("data") or [])]
                entry["models"] = models
                entry["reachable"] = bool(models)
                entry["detail"] = ", ".join(m for m in models if m)
                st = tunnel_status(prof, name) if prof.get("transport") == "ssh-tunnel" else {}
                if st:
                    entry["tunnel"] = st
        except (CfgError, Unreachable) as exc:
            entry["detail"] = str(exc)
        if not entry["reachable"]:
            worst = max(worst, EXIT_UNREACHABLE)
        out[name] = entry
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for name, entry in out.items():
            mark = "OK " if entry["reachable"] else "NO "
            print(f"{mark}{name:<10} {entry['kind']:<7} {entry['detail']}")
    return worst


def cmd_run(cfg: dict, args) -> int:
    if getattr(args, "route", False):
        name, prof = route_profile(cfg, args.sensitivity)
    else:
        name, prof = pick_profile(cfg, args.profile)
    check_policy(name, prof, args.sensitivity)
    prompt = read_prompt(args)
    res = run_profile(name, prof, prompt, args.system, args.temperature, args.max_tokens, args.timeout)
    audit({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": name,
        "model": res.get("model"),
        "sensitivity": args.sensitivity,
        "prompt_chars": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "completion_tokens": (res.get("usage") or {}).get("completion_tokens"),
        "ms": res.get("ms"),
    })
    print_result(res, args.json)
    return EXIT_OK


DOCPIPE_DEFAULT = Path(os.getenv("DOCPIPE") or (Path.home() / "Repos/docpipe/docpipe.py"))
REMOTE = {
    "python": r"C:\Users\usuario\AppData\Local\Programs\Python\Python312\python.exe",
    "script": r"C:\Users\usuario\tools\docpipe\docpipe.py",
    "key": r"C:\Users\usuario\.llama\api.key",
    "base_url": "http://127.0.0.1:8080/v1",
}


def _docpipe_python() -> str:
    venv = DOCPIPE_DEFAULT.parent / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def _preflight_remote(remote_mod, host: dict) -> None:
    """Comprueba Python, script y pypdf en Perdigon antes de lanzar nada."""
    py = REMOTE["python"]
    script = REMOTE["script"]
    out, _ = remote_mod.ssh_out(host, 'if exist "' + py + '" (echo PYOK) else (echo PYNO)')
    if "PYOK" not in out:
        raise CfgError("Perdigon no tiene Python en " + py)
    out, _ = remote_mod.ssh_out(host, 'if exist "' + script + '" (echo OK) else (echo NO)')
    if "OK" not in out:
        raise CfgError(
            "docpipe no esta desplegado en Perdigon: copia docpipe.py a " + script
            + " y ejecuta: " + py + " -m pip install --user pypdf")
    out, _ = remote_mod.ssh_out(
        host, '"' + py + '" -c "import pypdf,sys; sys.stdout.write(pypdf.__version__)"')
    if not re.match(r"^\d+\.", out.strip()):
        raise CfgError("falta pypdf en el Python de Perdigon: " + py + " -m pip install --user pypdf")


def cmd_docs(cfg: dict, args) -> int:
    name, prof = pick_profile(cfg, args.profile)
    check_policy(name, prof, "local-only")
    if prof.get("kind", "openai") != "openai":
        raise CfgError(f"el perfil '{name}' no es un endpoint OpenAI-compatible")
    if not DOCPIPE_DEFAULT.exists():
        raise CfgError(f"docpipe no encontrado en {DOCPIPE_DEFAULT} (define DOCPIPE)")

    common = ["scan", "--path", args.path, "--kind", args.kind]
    if args.out:
        common += ["--out", args.out]
    if args.limit:
        common += ["--limit", str(args.limit)]
    if args.since:
        common += ["--since", args.since]
    if args.dry_run:
        common += ["--dry-run"]
    if args.json:
        common += ["--json"]

    env = dict(os.environ)
    if args.source == "local":
        base = endpoint(prof, name)          # abre el túnel si hace falta
        key = api_key(prof)
        if key:
            env["DOCPIPE_API_KEY"] = key
        cmd = [_docpipe_python(), str(DOCPIPE_DEFAULT), *common,
               "--base-url", base, "--model", prof["model"]]
        if key:
            cmd += ["--api-key-env", "DOCPIPE_API_KEY"]
    else:
        remote = netconn()
        host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
        _preflight_remote(remote, host)
        args_txt = " ".join('"' + c + '"' for c in common)
        remote_cmd = ('"' + REMOTE["python"] + '" "' + REMOTE["script"] + '" ' + args_txt
                      + ' --base-url ' + REMOTE["base_url"]
                      + ' --model ' + prof["model"]
                      + ' --api-key-file "' + REMOTE["key"] + '"')
        cmd = remote.ssh_base(host) + [remote_cmd]

    audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cmd": "docs", "profile": name,
           "source": args.source, "kind": args.kind, "path": args.path,
           "dry_run": bool(args.dry_run)})
    proc = subprocess.run(cmd, env=env)
    return proc.returncode



def _stream_chat(base_url: str, model: str, messages: list[dict], key: str | None,
                 max_tokens: int, timeout: int, show_reasoning: bool):
    """Un turno con streaming SSE. Devuelve (texto, razonamiento, usage)."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
        "max_tokens": max_tokens,
        "reasoning_effort": "low",
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    content, reasoning, usage = [], [], {}
    finish = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        reasoning.append(delta["reasoning_content"])
                        if show_reasoning:
                            sys.stdout.write(delta["reasoning_content"])
                            sys.stdout.flush()
                    if delta.get("content"):
                        content.append(delta["content"])
                        sys.stdout.write(delta["content"])
                        sys.stdout.flush()
    except urllib.error.HTTPError as exc:
        raise Unreachable(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, OSError) as exc:
        raise Unreachable(f"sin conexión a {base_url}: {exc}")
    return "".join(content), "".join(reasoning), usage, finish



def _load_context(path: str, budget: int, max_docs: int) -> tuple[str, str, int]:
    """Extrae el texto de un path con docpipe y lo prepara como contexto.

    Devuelve (bloque, resumen legible, nº documentos). El texto NO se imprime.
    """
    if not DOCPIPE_DEFAULT.exists():
        raise CfgError(f"docpipe no encontrado en {DOCPIPE_DEFAULT} (define DOCPIPE)")
    cmd = [_docpipe_python(), str(DOCPIPE_DEFAULT), "extract", "--path", path,
           "--max-chars", str(budget), "--limit", str(max_docs + 40)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise CfgError(f"docpipe falló: {(proc.stderr or proc.stdout).strip()[:200]}")
    blocks, total, skipped, names = [], 0, 0, []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("status") != "ok" or not rec.get("text"):
            skipped += 1
            continue
        if len(blocks) >= max_docs or total + len(rec["text"]) > budget:
            skipped += 1
            continue
        blocks.append(f'### {rec["name"]}\n{rec["text"]}')
        names.append(rec["name"])
        total += len(rec["text"])
    if not blocks:
        raise CfgError(f"no se pudo extraer texto de {path} ({skipped} ficheros omitidos)")
    resumen = (f"contexto: {len(blocks)} documentos ({total} caracteres)"
               + (f", {skipped} omitidos (imagen/OCR, vacíos o fuera de presupuesto)" if skipped else ""))
    cabecera = (
        "## DOCUMENTOS YA CARGADOS\n"
        f"La ruta solicitada es `{path}` y su texto ya está incluido más abajo, "
        "extraído del disco local en esta misma sesión.\n"
        "Ficheros incluidos: " + ", ".join(names) + "\n"
        "REGLA OBLIGATORIA: no respondas que no puedes acceder a ficheros, ni pidas que "
        "te los suban o te peguen los datos: ya los tienes. Trabaja sobre el texto de "
        "abajo, cita el fichero del que sale cada dato y, si un dato no aparece, dilo.\n"
    )
    return cabecera + "\n\n".join(blocks), resumen, len(blocks)




FS_TOOLS = [
    {"type": "function", "function": {
        "name": "list_docs",
        "description": "Lista los documentos disponibles bajo la raiz permitida (nombre y tamano).",
        "parameters": {"type": "object",
                       "properties": {"subdir": {"type": "string",
                                                 "description": "subcarpeta relativa, opcional"}}}}},
    {"type": "function", "function": {
        "name": "read_doc",
        "description": "Lee el texto de un documento bajo la raiz permitida (pdf, docx, eml, txt, md).",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "description": "ruta relativa a la raiz"}},
                       "required": ["path"]}}},
]

DOC_EXT = {".pdf", ".docx", ".eml", ".txt", ".md", ".csv", ".json"}


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def fs_list(root: Path, subdir: str = "", cap: int = 40) -> list:
    base = (root / subdir).resolve() if subdir else root.resolve()
    if not _inside(root.resolve(), base):
        raise ValueError("esa ruta queda fuera de la raiz permitida")
    if not base.is_dir():
        raise ValueError("no es un directorio: %s" % subdir)
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in DOC_EXT:
            out.append({"path": str(p.relative_to(root.resolve())),
                        "kb": round(p.stat().st_size / 1024)})
            if len(out) >= cap:
                break
    return out


def fs_read(root: Path, rel: str, cap: int = 20000) -> dict:
    p = (root / rel).resolve()
    if not _inside(root.resolve(), p):
        raise ValueError("esa ruta queda fuera de la raiz permitida")
    if not p.is_file():
        raise ValueError("no existe: %s" % rel)
    if p.suffix.lower() not in DOC_EXT:
        raise ValueError("formato no soportado: %s" % p.suffix)
    if not DOCPIPE_DEFAULT.exists():
        raise CfgError(f"docpipe no encontrado en {DOCPIPE_DEFAULT}")
    proc = subprocess.run([_docpipe_python(), str(DOCPIPE_DEFAULT), "extract",
                           "--path", str(p), "--max-chars", str(cap)],
                          capture_output=True, text=True, timeout=180)
    rec = json.loads(proc.stdout.splitlines()[0]) if proc.stdout.strip() else {}
    if rec.get("status") != "ok":
        return {"path": rel, "status": rec.get("status", "error"), "chars": 0,
                "nota": "sin capa de texto; necesitaria OCR"}
    return {"path": rel, "status": "ok", "chars": rec.get("chars"), "text": rec.get("text", "")}


WEB_TOOLS = [
    {"type": "function", "function": {
        "name": "fetch_source",
        "description": "Lee una fuente declarada (URL oficial conocida) y devuelve su texto. Usala en vez de inventar URLs o de buscar.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "nombre de la fuente declarada"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Busca en internet y devuelve resultados (titulo, url, extracto).",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"},
                                      "n": {"type": "integer", "description": "cuantos resultados (1-8)"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Descarga una URL concreta y devuelve su texto plano (max 6000 caracteres).",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _unwrap_ddg(href: str) -> str:
    """Los enlaces de DDG vienen envueltos: extrae la URL real del parametro uddg."""
    if "uddg=" not in href:
        return href if href.startswith("http") else ""
    qs = urllib.parse.urlparse(href if href.startswith("http") else "https:" + href).query
    target = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
    if not target or "duckduckgo.com/y.js" in target:
        return ""          # anuncio
    return target


def web_search(query: str, n: int = 5, timeout: int = 20) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    out, seen = [], set()
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
        real = _unwrap_ddg(m.group(1))
        if not real or real in seen:
            continue
        seen.add(real)
        out.append({"title": html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
                    "url": real})
    snippets = [html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()
                for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', raw, re.S)]
    for i, item in enumerate(out):
        if i < len(snippets):
            item["snippet"] = snippets[i][:300]
    return out[:max(1, min(int(n or 5), 8))]


def fetch_page(url: str, timeout: int = 20, cap: int = 6000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html_mod.unescape(re.sub(r"[ \t\r\f\v]+", " ", text))
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:cap]


FUENTES_POR_DEFECTO = Path(__file__).resolve().parent / "datos" / "fuentes.json"
FUENTES_PATH = None


def cargar_fuentes(ruta=None) -> dict:
    """Fuentes declaradas: nombre -> {url, desc}. Sin fuentes no hay nada que romper."""
    p = Path(ruta) if ruta else FUENTES_POR_DEFECTO
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


_ULTIMOS_RESULTADOS: list = []


def _ultimas_urls(n: int = 5) -> list:
    return [r.get("url") for r in _ULTIMOS_RESULTADOS[:n] if r.get("url")]


def _run_tool(call: dict, root: Path | None = None, permitidas=None) -> str:
    fn = (call.get("function") or {})
    name = fn.get("name")
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return json.dumps({"error": "argumentos no son JSON"})
    # --- alias: gpt-oss lleva web_search/fetch_page incorporadas y las llamará
    # aunque no se le ofrezcan. Sin herramientas web, "buscar" = buscar en disco.
    if root is not None and permitidas is not None and "web_search" not in permitidas:
        nombre_tool = name
        if nombre_tool == "web_search":
            q = (args.get("query") or "").lower()
            print('\n[doc] el modelo pide buscar "%s" -> busco en los documentos' % q[:60], flush=True)
            docs = fs_list(root)
            elegido = None
            for d in docs:
                stem = d["path"].split("/")[-1].lower()
                if stem and (stem in q or stem.rsplit(".", 1)[0] in q):
                    elegido = d["path"]
                    break
            out = {"aviso": "no hay buscador web en esta sesion; has buscado en los documentos",
                   "documentos": docs}
            if elegido:
                out["leido"] = fs_read(root, elegido)
                print('\n[doc] coincide "%s", lo leo' % elegido, flush=True)
            return json.dumps(out, ensure_ascii=False)
        if nombre_tool == "fetch_page":
            u = args.get("url") or ""
            rel = u
            try:
                rel = str(Path(u).resolve().relative_to(root.resolve()))
            except Exception:
                rel = u
            print('\n[doc] el modelo pide abrir "%s" -> lo leo del disco' % u[:60], flush=True)
            return json.dumps(fs_read(root, rel), ensure_ascii=False)
    if permitidas is not None and name not in permitidas:
        lista = ""
        if root is not None:
            try:
                lista = ("\nDocumentos disponibles ahora mismo: "
                         + ", ".join(d["path"] for d in fs_list(root)))
            except Exception:
                pass
        return ("No existe la herramienta '%s' en esta sesion. Las unicas disponibles son: %s."
                "%s\nUsala ahora." % (name, ", ".join(sorted(permitidas)), lista))
    try:
        if name == "list_docs":
            if root is None:
                return json.dumps({"error": "no hay raiz de documentos configurada"})
            docs = fs_list(root, args.get("subdir", ""))
            print("\n[doc] lista: %d documentos" % len(docs), flush=True)
            return json.dumps(docs, ensure_ascii=False)
        if name == "read_doc":
            if root is None:
                return json.dumps({"error": "no hay raiz de documentos configurada"})
            rel = args.get("path", "")
            print('\n[doc] lee: "%s"' % rel, flush=True)
            return json.dumps(fs_read(root, rel), ensure_ascii=False)
        if fn.get("name") == "web_search":
            q = args.get("query", "")
            print('\n[web] busca: "%s"' % q, flush=True)
            res = web_search(q, args.get("n", 5))
            _ULTIMOS_RESULTADOS[:] = res
            # El modelo pequeno ignora las URLs y se inventa rutas: se le entrega
            # ya leido el texto de los primeros resultados, que no puede ignorar.
            leidos = []
            for i, item in enumerate(res[:2]):
                try:
                    print("\n[web] lee resultado %d: %s" % (i + 1, item["url"]), flush=True)
                    texto = fetch_page(item["url"], cap=6000 if i == 0 else 3000)
                    leidos.append({"url": item["url"], "titulo": item.get("title", ""),
                                   "texto": texto})
                except Exception as exc:
                    leidos.append({"url": item["url"], "error": str(exc)[:100]})
            return json.dumps({"resultados": [{k: v for k, v in r.items() if k != "texto"}
                                              for r in res],
                               "ya_leidos": leidos,
                               "instruccion": "el texto de los primeros resultados ya esta en "
                                              "ya_leidos; responde con el. Si necesitas otra "
                                              "pagina, usa fetch_page con una URL EXACTA de "
                                              "resultados."},
                              ensure_ascii=False)
        if fn.get("name") == "fetch_source":
            nombre = (args.get("name") or "").strip()
            fuentes = cargar_fuentes(FUENTES_PATH)
            f = fuentes.get(nombre)
            if not isinstance(f, dict) or not f.get("url"):
                return json.dumps({"error": "fuente '" + nombre + "' no declarada",
                                   "disponibles": [k for k, v in fuentes.items()
                                                   if isinstance(v, dict)]}, ensure_ascii=False)
            print("\n[fuente] " + nombre + " -> " + f["url"], flush=True)
            try:
                return fetch_page(f["url"], cap=20000)
            except Exception as exc:
                return json.dumps({"error": "no se pudo leer " + f["url"] + ": " + str(exc)[:120]},
                                  ensure_ascii=False)
        if fn.get("name") == "fetch_page":
            u = (args.get("url") or "").strip()
            print(f"[web] abre: {u}")
            try:
                return fetch_page(u)
            except Exception as exc:
                reales = _ultimas_urls()
                print(f"[web] fallo al abrir; devuelvo {len(reales)} URLs reales")
                return json.dumps({"error": "no se pudo abrir %s (%s)" % (u, str(exc)[:120]),
                                   "urls_validas_de_la_ultima_busqueda": reales,
                                   "instruccion": "esa URL no existe; abre una de estas"},
                                  ensure_ascii=False)
        return json.dumps({"error": f"herramienta desconocida: {name}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)[:200]})


def chat_with_tools(base_url: str, model: str, messages: list[dict], key: str | None,
                    max_tokens: int, timeout: int, max_rounds: int = 4,
                    tools: list | None = None, root: Path | None = None):
    """Bucle de herramientas: el modelo pide búsquedas, se ejecutan y vuelve a responder."""
    usage_total = {}
    for ronda in range(max_rounds + 1):
        ultima = ronda == max_rounds
        if ultima:
            messages = messages + [{"role": "user", "content":
                "Ya no busques más. Responde ahora con la información que hayas obtenido."}]
        payload = {"model": model, "messages": messages, "temperature": 0.3,
                   "max_tokens": max_tokens, "reasoning_effort": "low",
                   "tools": tools or WEB_TOOLS, "tool_choice": "none" if ultima else "auto"}
        resp = http_json(base_url.rstrip("/") + "/chat/completions", payload, key, timeout)
        for k, v in (resp.get("usage") or {}).items():
            if isinstance(v, int):
                usage_total[k] = usage_total.get(k, 0) + v
        msg = ((resp.get("choices") or [{}])[0].get("message")) or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return msg.get("content") or "", usage_total
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        permitidas = {t["function"]["name"] for t in (tools or WEB_TOOLS)}
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": _run_tool(call, root, permitidas)})
    return "(se agotaron las rondas de búsqueda sin respuesta final)", usage_total


REFUSAL_RE = re.compile(
    r"no (?:tengo|puedo) acceder|no puedo leer|no tengo acceso|necesitar[íi]a poder leer|"
    r"subir los pdf|comparte los (?:archivos|datos)|no dispongo de acceso", re.I)


def _parece_negativa(texto: str) -> bool:
    return bool(REFUSAL_RE.search(texto or ""))


CHAT_HELP = """comandos:
  /salir            termina
  /reset            olvida la conversación (mantiene el sistema)
  /sistema <texto>  fija el mensaje de sistema
  /razonamiento     muestra u oculta el canal de razonamiento
  /docs             lista los documentos de --root
  /leer <ruta>...   carga uno o varios documentos en el contexto (pdf, docx, eml, txt)
  /guardar <ruta>   escribe la transcripción (0600)
  /estado           perfil, modelo, turnos y tokens
"""



REMOTE_AGENT = {
    "python": r"C:\Users\usuario\AppData\Local\Programs\Python\Python312\python.exe",
    "driver": r"C:\Users\usuario\tools\agent\agent_driver.py",
}



def _ps(remote, host, cmd):
    out, _ = remote.ssh_out(host, 'powershell -NoProfile -Command "' + cmd + '"')
    return out



def lifecycle_conf(prof: dict) -> dict:
    return prof.get("lifecycle") or {}


def ensure_host(prof: dict, name: str, timeout: int = 300) -> bool:
    """Despierta el host por WoL si está apagado. Devuelve True si lo hemos despertado."""
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    if remote.ssh_ready(host):
        return False
    if not lifecycle_conf(prof).get("wake", True):
        raise Unreachable(f"{prof.get('host')} no responde y wake está desactivado")
    print(f"[host] {prof.get('host')} apagado: enviando Wake-on-LAN…", file=sys.stderr)
    rc = remote.cmd_ensure(host, prof.get("host"), timeout)
    if rc != 0:
        raise Unreachable(f"no se pudo despertar/entrar en {prof.get('host')} (código {rc})")
    return True


def shutdown_host(prof: dict, name: str, delay: int = 8) -> None:
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    print(f"[host] apagando {prof.get('host')} en {delay}s", file=sys.stderr)
    remote.ssh_out(host, f"shutdown /s /f /t {delay}")


def shutdown_policy(prof: dict, woken: bool) -> bool:
    """never | always | if_woken (por defecto: solo si lo encendimos nosotros)."""
    modo = (lifecycle_conf(prof).get("shutdown") or "if_woken").lower()
    if modo == "always":
        return True
    if modo == "never":
        return False
    return woken


def model_port(prof: dict) -> int:
    return int(prof.get("remote_port", 8080))


def model_up_state(prof: dict, name: str) -> bool:
    """¿escucha el servidor en el loopback del host? (sin abrir túnel)"""
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    out = _ps(remote, host,
              "if (Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue) "
              "{ Write-Output UP } else { Write-Output DOWN }" % model_port(prof))
    return "UP" in out


def model_healthy(prof: dict, name: str) -> bool:
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    out, _ = remote.ssh_out(host, "curl.exe -s -m 3 http://127.0.0.1:%d/health" % model_port(prof))
    return '"status":"ok"' in out.replace(" ", "")


def model_start(prof: dict, name: str, timeout: int = 300) -> bool:
    """Devuelve True si lo hemos arrancado nosotros."""
    if model_healthy(prof, name):
        return False
    task = ((prof.get("lifecycle") or {}).get("task")) or "llama-server"
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    print(f"[modelo] arrancando '{task}' en {prof.get('host')}…", file=sys.stderr)
    remote.ssh_out(host, 'schtasks /run /tn "%s"' % task)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if model_healthy(prof, name):
            print(f"[modelo] listo en {int(timeout - (deadline - time.time()))} s", file=sys.stderr)
            return True
        time.sleep(3)
    raise Unreachable(f"el modelo no respondió en {timeout}s (tarea {task})")


def model_stop(prof: dict, name: str) -> None:
    task = ((prof.get("lifecycle") or {}).get("task")) or "llama-server"
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    remote.ssh_out(host, 'schtasks /end /tn "%s"' % task)
    for _ in range(12):
        if not model_up_state(prof, name):
            print("[modelo] parado, VRAM liberada", file=sys.stderr)
            return
        time.sleep(1)
    # `schtasks /end` cierra la tarea pero a veces deja el proceso hijo vivo
    print("[modelo] la tarea terminó pero el proceso sigue: rematando", file=sys.stderr)
    remote.ssh_out(host, 'powershell -NoProfile -Command "Get-Process llama-server '
                         '-ErrorAction SilentlyContinue | Stop-Process -Force"')
    for _ in range(10):
        if not model_up_state(prof, name):
            print("[modelo] parado, VRAM liberada", file=sys.stderr)
            return
        time.sleep(1)
    print("[modelo] aviso: sigue escuchando tras el remate", file=sys.stderr)


def lifecycle_auto(prof: dict) -> bool:
    return bool(((prof.get("lifecycle") or {}).get("auto_stop")))


def cmd_model(cfg: dict, args) -> int:
    name, prof = pick_profile(cfg, args.profile)
    if args.action == "status":
        up, healthy = model_up_state(prof, name), model_healthy(prof, name)
        print(f"{name}: escucha={'sí' if up else 'no'} salud={'ok' if healthy else 'no'}")
        return EXIT_OK if healthy else EXIT_UNREACHABLE
    if args.action == "up":
        woken = ensure_host(prof, name, args.timeout)
        started = model_start(prof, name, args.timeout)
        print(f"{name}: host {'despertado' if woken else 'ya estaba'} · modelo "
              f"{'arrancado' if started else 'ya en marcha'}")
        return EXIT_OK
    if args.action == "off":
        model_stop(prof, name)
        shutdown_host(prof, name)
        print(f"{name}: parado y apagando el equipo")
        return EXIT_OK
    model_stop(prof, name)
    print(f"{name}: parado")
    return EXIT_OK


def cmd_chat_remoto(cfg: dict, args) -> int:
    """Delega la sesión completa en el host: allí se infiere, se decide y se busca."""
    name, prof = pick_profile(cfg, args.profile)
    check_policy(name, prof, args.sensitivity)
    if args.context and (args.context.startswith("/") or args.context.startswith("~")):
        raise CfgError("con --source equipo, --context debe ser una ruta del host "
                       "(p. ej. C:\\Users\\usuario\\Documents); los documentos del Mac van "
                       "con --source local")
    remote = netconn()
    host = netconn_host(remote, prof.get("ssh_host") or prof.get("host"))
    argv = [REMOTE_AGENT["python"], REMOTE_AGENT["driver"],
            "--model", prof["model"],
            "--base-url", "http://127.0.0.1:" + str(int(prof.get("remote_port", 8080))) + "/v1",
            "--api-key-file", prof.get("api_key", {}).get("path", ""),
            "--max-tokens", str(args.max_tokens),
            "--tools", args.tools,
            "--web-rounds", str(args.web_rounds)]
    if args.context:
        argv += ["--context", args.context]
    if getattr(args, "root", None):
        argv += ["--root", args.root]
    if args.system:
        argv += ["--system", args.system]
    if args.prompt:
        argv += ["--prompt", args.prompt]
    if args.show_reasoning:
        argv += ["--show-reasoning"]

    quoted = " ".join('"' + a.replace('"', '\"') + '"' for a in argv)
    cmd = remote.ssh_base(host) + ([] if args.prompt else ["-t"]) + [quoted]
    audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cmd": "chat", "profile": name,
           "remote": True, "tools": args.tools, "one_shot": bool(args.prompt)})
    try:
        return subprocess.run(cmd, stdin=None if args.prompt else sys.stdin).returncode
    except KeyboardInterrupt:
        return EXIT_ERROR


def cmd_chat(cfg: dict, args) -> int:
    if getattr(args, "source", "local") == "equipo":
        return cmd_chat_remoto(cfg, args)
    name, prof = pick_profile(cfg, args.profile)
    check_policy(name, prof, args.sensitivity)
    if prof.get("kind", "openai") != "openai":
        raise CfgError(f"el perfil '{name}' no es un endpoint OpenAI-compatible")
    base = endpoint(prof, name)
    key = api_key(prof)
    defaults = prof.get("defaults") or {}

    # El `system` del perfil está afinado para extracción (devuelve JSON); en chat
    # solo se usa lo que pida el usuario con --system.
    system = args.system
    global FUENTES_PATH
    FUENTES_PATH = getattr(args, "fuentes", None)
    _t = [x.strip() for x in (getattr(args, "tools", None) or "none").split(",") if x.strip()]
    usa_web = "web" in _t
    usa_docs_tool = "docs" in _t
    root = Path(args.root).expanduser().resolve() if getattr(args, "root", None) else None
    if root is not None and not root.is_dir():
        raise CfgError(f"--root no es un directorio: {root}")
    if root is not None:
        system = ((system + "\n\n") if system else "") + (
            "El usuario carga documentos con el comando /leer; el texto queda DENTRO de la "
            "conversación, en un mensaje con la cabecera '## DOCUMENTOS YA CARGADOS'.\n"
            "REGLA OBLIGATORIA: si pregunta por esos documentos, por una carpeta o por una "
            "ruta, responde con el contenido ya cargado. NUNCA contestes que no tienes acceso "
            "al sistema de ficheros, ni pidas que se peguen o exporten los datos: ya están en "
            "el contexto y basta con leerlos de ahí.")
    if root is not None and usa_docs_tool:
        system = ((system + "\n\n") if system else "") + (
            f"Tienes acceso de lectura a los documentos bajo `{root}`.\n"
            "HERRAMIENTAS DISPONIBLES EN ESTA SESION: list_docs (sin argumentos o con "
            "subdir) y read_doc (con path relativo).\n"
            "NO EXISTE ninguna herramienta de búsqueda web en esta sesión; no llames a "
            "web_search ni a tools parecidas.\n"
            "Flujo obligatorio: 1) list_docs para ver qué hay, 2) read_doc del documento "
            "concreto, 3) responder citando el fichero. Nunca digas que no puedes acceder "
            "a ficheros: puedes, con esas dos herramientas.")
    regla_web = (
        "\nLos documentos cubren SOLO lo que está escrito en ellos. Si la pregunta pide "
        "algo del mundo exterior que no aparece ahí (precios o tarifas vigentes, ofertas, "
        "condiciones comerciales, datos de terceros), DEBES usar web_search antes de "
        "responder: no contestes de memoria ni digas que no puedes consultar.\n"
        if usa_web else "")
    if getattr(args, "context", None):
        block, resumen, _n = _load_context(args.context, args.context_max_chars, args.context_max_docs)
        print(resumen)
        system = ((system + "\n\n") if system else "") + (
            "Tienes el TEXTO de los siguientes documentos locales, extraído del disco. "
            "Responde a partir de ellos, cita el nombre del fichero en el que te apoyas "
            "y di claramente cuándo un dato no aparece." + regla_web + "\n\n" + block)
    elif usa_web:
        system = ((system + "\n\n") if system else "") + (
            "Tienes herramientas de búsqueda web. Úsalas siempre que la respuesta dependa de "
            "datos actuales (precios, tarifas, condiciones, noticias) y cita las URLs.")
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    herramientas = (FS_TOOLS if (root and usa_docs_tool) else []) + (WEB_TOOLS if usa_web else [])
    history: list[tuple[str, str]] = []
    show_reasoning = args.show_reasoning
    context_loaded = bool(getattr(args, "context", None))
    cargados: dict = {}
    stats = {"turns": 0, "prompt": 0, "completion": 0}

    def turn(prompt: str) -> str:
        messages.append({"role": "user", "content": prompt})
        started = time.time()
        if herramientas:
            text, usage = chat_with_tools(base, prof["model"], messages, key,
                                          args.max_tokens, args.timeout, args.web_rounds,
                                          tools=herramientas, root=root)
            print(text)
            finish, reasoning = "stop", ""
        else:
            text, reasoning, usage, finish = _stream_chat(
                base, prof["model"], messages, key, args.max_tokens, args.timeout, show_reasoning)
        if (context_loaded or cargados) and _parece_negativa(text):
            # El modelo tiene el texto pero niega el acceso: se le recuerda una vez.
            sys.stdout.write("\n[reintento: el modelo negó acceso teniendo el contexto]\n")
            messages.append({"role": "assistant", "content": text})
            cargados_txt = ", ".join(cargados) if cargados else "los ya cargados"
            messages.append({"role": "user", "content":
                "El texto de esos documentos ya está en tu contexto, arriba, en el bloque "
                "'## DOCUMENTOS YA CARGADOS' (" + cargados_txt + "), extraído del disco por "
                "la herramienta. No necesitas acceso al sistema de archivos, ni que nadie te "
                "pegue datos, ni que exporte nada. Responde AHORA usando ese texto: importes, "
                "consumos, periodos, potencias y condiciones que aparezcan, citando el fichero. "
                "Si algo no está en el texto, dilo; no pidas que se te envíe."})
            text, reasoning, usage, finish = _stream_chat(
                base, prof["model"], messages, key, args.max_tokens, args.timeout, show_reasoning)
            messages.pop()
        if not text.strip():
            if finish == "length":
                aviso = f"(cortado: se agotaron los {args.max_tokens} tokens razonando. " \
                        f"Sube --max-tokens o usa /razonamiento para verlo)"
            else:
                aviso = f"(el modelo no devolvió texto; razonó {len(reasoning)} caracteres " \
                        f"y paró con finish_reason={finish}. Prueba /razonamiento o reformula)"
            sys.stdout.write(aviso + "\n")
            # no se guarda un turno vacío en el historial: envenenaría el contexto
            messages.pop()
            return ""
        elapsed = time.time() - started
        history.append((prompt, text))
        stats["turns"] += 1
        stats["prompt"] += usage.get("prompt_tokens", 0)
        stats["completion"] += usage.get("completion_tokens", 0)
        sys.stdout.write(f"\n[{elapsed:.1f} s · {usage.get('completion_tokens', '?')} tok]\n\n")
        sys.stdout.flush()
        return text

    audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "cmd": "chat", "profile": name,
           "one_shot": bool(args.prompt)})

    if args.prompt:
        turn(args.prompt)
        return EXIT_OK

    print(f"modelgate chat · {name} · {prof['model']} · {base}")
    print("escribe /salir para terminar, /ayuda para comandos\n")
    while True:
        try:
            line = input("tú> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            rest = rest.strip()
            if cmd in ("/salir", "/exit", "/quit"):
                break
            if cmd == "/ayuda" or cmd == "/help":
                print(CHAT_HELP)
            elif cmd == "/reset":
                messages = [m for m in messages if m["role"] == "system"]
                history.clear()
                print("(conversación olvidada)")
            elif cmd == "/sistema":
                messages = [m for m in messages if m["role"] != "system"]
                messages.insert(0, {"role": "system", "content": rest})
                print("(sistema actualizado)")
            elif cmd == "/razonamiento":
                show_reasoning = not show_reasoning
                print(f"(razonamiento {'visible' if show_reasoning else 'oculto'})")
            elif cmd == "/guardar":
                if not rest:
                    print("uso: /guardar <ruta>")
                    continue
                path = Path(rest).expanduser()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for q, a in history:
                        fh.write(f"## tú\n{q}\n\n## {prof['model']}\n{a}\n\n")
                print(f"(transcripción en {path})")
            elif cmd == "/leer":
                if not rest:
                    print("uso: /leer <ruta> [<ruta> ...]")
                    continue
                try:
                    pedazos = shlex.split(rest)
                except ValueError:
                    pedazos = rest.split()
                for pedazo in pedazos:
                    try:
                        ruta = Path(pedazo).expanduser()
                        if ruta.is_absolute():
                            # ruta tecleada por el usuario: se permite fuera de --root
                            info = fs_read(ruta.parent, ruta.name)
                        else:
                            info = fs_read(root if root is not None else Path.cwd(), pedazo)
                        if info.get("status") != "ok":
                            print("(no se pudo leer %s: %s%s)"
                                  % (pedazo, info.get("status"),
                                     "; " + info["nota"] if info.get("nota") else ""))
                            continue
                        if info["path"] in cargados:
                            print("(ya estaba cargado %s: %d caracteres)"
                                  % (info["path"], cargados[info["path"]]))
                            continue
                        messages.append({"role": "user", "content":
                                         "## DOCUMENTOS YA CARGADOS\n"
                                         "El texto siguiente YA está en esta conversación: no hace falta "
                                         "acceso al sistema de ficheros ni que se pegue nada. Es la fuente.\n\n"
                                         "### %s (%d caracteres)\n%s"
                                         % (info["path"], info["chars"], info["text"])})
                        cargados[info["path"]] = info["chars"]
                        print("(cargado %s: %d caracteres · total en contexto %d)"
                              % (info["path"], info["chars"], sum(cargados.values())))
                    except Exception as exc:
                        print("(error leyendo %s: %s)" % (pedazo, exc))
            elif cmd == "/docs":
                if root is None:
                    print("(no hay --root; usa --root <carpeta>)")
                    continue
                try:
                    for d in fs_list(root):
                        print("  %-50s %6d KB" % (d["path"], d["kb"]))
                except Exception as exc:
                    print(f"(error: {exc})")
            elif cmd == "/estado":
                print(f"perfil {name} · modelo {prof['model']} · transporte "
                      f"{prof.get('transport', 'http')} · turnos {stats['turns']} · "
                      f"tokens {stats['prompt']} in / {stats['completion']} out")
            else:
                print(f"comando desconocido: {cmd} (prueba /ayuda)")
            continue
        try:
            turn(line)
        except Unreachable as exc:
            print(f"\n[error] {exc}\n")
        except KeyboardInterrupt:
            print("\n(interrumpido)\n")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="modelgate", description="Enruta trabajo entre modelo local y cloud.")
    p.add_argument("--version", action="version", version=f"modelgate {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="perfiles configurados")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="¿responde cada destino?")
    s.add_argument("profiles", nargs="*")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("tunnel", help="gestiona el túnel SSH")
    s.add_argument("action", choices=["up", "down", "status", "off"])
    s.add_argument("profile", nargs="?", default=None)
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("model", help="arranca, para o consulta el modelo en el host")
    s.add_argument("action", choices=["up", "down", "status", "off"])
    s.add_argument("--profile", default=None)
    s.add_argument("--timeout", type=int, default=300)

    s = sub.add_parser("chat", help="sesión conversacional con el modelo (streaming)")
    s.add_argument("--profile", default=None)
    s.add_argument("--prompt", help="un solo turno y sale (para scripts)")
    s.add_argument("--system")
    s.add_argument("--sensitivity", default="cloud-ok", choices=list(SENSITIVITIES))
    s.add_argument("--max-tokens", type=int, default=2048)
    s.add_argument("--timeout", type=int, default=600)
    s.add_argument("--show-reasoning", action="store_true")
    s.add_argument("--keep-alive", action="store_true", help="no parar el modelo al terminar")
    s.add_argument("--source", choices=["local", "equipo"], default="local",
                   help="local=infiere por el túnel; equipo=todo ocurre en el host")
    s.add_argument("--fuentes", default=None,
                   help="JSON con fuentes declaradas (por defecto datos/fuentes.json)")
    s.add_argument("--tools", default="none",
                   help="herramientas del modelo: web, docs o web,docs (por defecto ninguna)")
    s.add_argument("--web-rounds", type=int, default=4)
    s.add_argument("--context", help="carpeta o fichero cuyos documentos se cargan en el contexto")
    s.add_argument("--root", help="raíz de documentos que el modelo puede listar y leer bajo demanda")
    s.add_argument("--context-max-chars", type=int, default=40000)
    s.add_argument("--context-max-docs", type=int, default=20)

    s = sub.add_parser("docs", help="analiza documentos del path con el modelo local")
    s.add_argument("path")
    s.add_argument("--source", choices=["local", "equipo"], default="local",
                   help="dónde están los documentos (local=este Mac)")
    s.add_argument("--kind", default="generico",
                   choices=["factura", "recibo", "contrato", "correo", "generico"])
    s.add_argument("--out")
    s.add_argument("--limit", type=int)
    s.add_argument("--since")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--profile", default=None)
    s.add_argument("--json", action="store_true")
    s.add_argument("--keep-alive", action="store_true")

    for cmd, help_ in (("run", "ejecuta en un perfil"), ("route", "elige perfil por sensibilidad")):
        s = sub.add_parser(cmd, help=help_)
        if cmd == "run":
            s.add_argument("profile", nargs="?", default=None)
        s.add_argument("--prompt")
        s.add_argument("--file")
        s.add_argument("--system")
        s.add_argument("--sensitivity", default="cloud-ok", choices=list(SENSITIVITIES))
        s.add_argument("--temperature", type=float)
        s.add_argument("--max-tokens", type=int)
        s.add_argument("--timeout", type=int, default=300)
        s.add_argument("--json", action="store_true")
        s.add_argument("--keep-alive", action="store_true")
        if cmd == "route":
            s.set_defaults(route=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        if args.cmd == "list":
            return cmd_list(cfg, args.json)
        if args.cmd == "status":
            return cmd_status(cfg, args.profiles, args.json)
        if args.cmd == "tunnel":
            name, prof = pick_profile(cfg, args.profile)
            if args.action == "down":
                out = tunnel_down(name)
            elif args.action == "status":
                out = tunnel_status(prof, name)
            else:
                if prof.get("transport") != "ssh-tunnel":
                    raise CfgError(f"el perfil '{name}' no usa túnel")
                out = tunnel_up(prof, name)
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
            elif args.action == "down":
                print(f"{name}: túnel detenido (pid={out['pid']})")
            else:
                print(f"{name}: pid={out['pid']} vivo={out['alive']} "
                      f"puerto={out['local_port']} abierto={out['port_open']}")
            return EXIT_OK if (args.action == "down" or out["port_open"] or out["alive"]) else EXIT_UNREACHABLE
        if args.cmd == "model":
            return cmd_model(cfg, args)
        if args.cmd in ("chat", "docs", "run", "route"):
            # ciclo bajo demanda: se enciende si hace falta y se apaga si lo encendimos
            name0, prof0 = pick_profile(cfg, getattr(args, "profile", None))
            gestionar = lifecycle_auto(prof0) and not getattr(args, "keep_alive", False)
            despertado = ensure_host(prof0, name0) if gestionar else False
            iniciado = model_start(prof0, name0) if gestionar else False
            try:
                if args.cmd == "chat":
                    return cmd_chat(cfg, args)
                if args.cmd == "docs":
                    return cmd_docs(cfg, args)
                return cmd_run(cfg, args)
            finally:
                if iniciado:
                    model_stop(prof0, name0)
                if gestionar and shutdown_policy(prof0, despertado) and args.cmd == "chat":
                    shutdown_host(prof0, name0)
        raise CfgError(f"comando desconocido {args.cmd}")
    except PolicyError as exc:
        print(f"modelgate: política: {exc}", file=sys.stderr)
        return EXIT_POLICY
    except CfgError as exc:
        print(f"modelgate: configuración: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Unreachable as exc:
        print(f"modelgate: destino inaccesible: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except KeyboardInterrupt:
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
