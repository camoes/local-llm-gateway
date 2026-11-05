#!/usr/bin/env python3
"""agent_driver — sesión de chat con el modelo local, ejecutada EN el host.

Todo ocurre aquí: la inferencia (llama-server en loopback), la decisión de buscar
y la búsqueda misma. El cliente remoto solo ve texto. Sin dependencias externas.

    agent_driver.py                        # REPL interactivo
    agent_driver.py --prompt "..."         # un turno y sale
    agent_driver.py --tools web            # permite búsquedas en internet
    agent_driver.py --context C:\ruta      # carga documentos de ese path
"""

from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import shlex
import os
import re

REFUSAL_RE = re.compile(
    r"no (?:tengo|puedo) acceder|no puedo leer|no tengo acceso|necesitar[ií]a poder leer|"
    r"subir los pdf|comparte los (?:archivos|datos)|no dispongo de acceso", re.I)
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
APP_HOME = Path(os.getenv("AGENT_HOME") or (Path.home() / ".agent"))
DEFAULT_KEY = Path.home() / ".llama" / "api.key"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

FS_TOOLS = [
    {"type": "function", "function": {
        "name": "list_docs",
        "description": "Lista los documentos disponibles bajo la raiz permitida.",
        "parameters": {"type": "object",
                       "properties": {"subdir": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "read_doc",
        "description": "Lee el texto de un documento bajo la raiz permitida.",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
]
DOC_EXT = {".pdf", ".docx", ".eml", ".txt", ".md", ".csv", ".json"}

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
                                      "n": {"type": "integer"}},
                       "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page",
        "description": "Descarga una URL y devuelve su texto plano (max 6000 caracteres).",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]}}},
]


def _out():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _unwrap_ddg(href: str) -> str:
    if "uddg=" not in href:
        return href if href.startswith("http") else ""
    qs = urllib.parse.urlparse(href if href.startswith("http") else "https:" + href).query
    target = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
    if not target or "duckduckgo.com/y.js" in target:
        return ""
    return target


def _ddg_search(query: str, n: int, timeout: int) -> list:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Accept-Language": "es-ES,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    out, seen = [], set()
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
        real = _unwrap_ddg(m.group(1))
        if not real or real in seen:
            continue
        seen.add(real)
        out.append({"title": html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
                    "url": real})
    snippets = [html_mod.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                for t in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', raw, re.S)]
    for i, item in enumerate(out):
        if i < len(snippets):
            item["snippet"] = snippets[i][:300]
    return out[:n]


def _unwrap_bing(href: str) -> str:
    """Bing envuelve los enlaces en bing.com/ck/a?u=a1<base64url de la URL>."""
    href = html_mod.unescape(href)
    if "bing.com/ck/a" not in href:
        return href if href.startswith("http") else ""
    params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    u = params.get("u", [""])[0]
    if not u.startswith("a1"):
        return ""
    payload = u[2:]
    try:
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")
    except Exception:
        return ""


def _bing_search(query: str, n: int, timeout: int) -> list:
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Accept-Language": "es-ES,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    out, seen = [], set()
    for block in re.findall(r'<li class="b_algo".*?</li>', raw, re.S):
        m = re.search(r'<h2>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        real = _unwrap_bing(m.group(1))
        if not real.startswith("http") or "bing.com" in real or real in seen:
            continue
        seen.add(real)
        item = {"title": html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
                "url": real}
        snip = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        if snip:
            item["snippet"] = html_mod.unescape(re.sub(r"<[^>]+>", "", snip.group(1))).strip()[:300]
        out.append(item)
    return out[:n]


def web_search(query: str, n: int = 5, timeout: int = 20) -> list:
    """DuckDuckGo y, si no devuelve nada (202/desafío), Bing."""
    n = max(1, min(int(n or 5), 8))
    for nombre, backend in (("duckduckgo", _ddg_search), ("bing", _bing_search)):
        try:
            res = backend(query, n, timeout)
        except Exception as exc:
            print("[web] %s falló: %s" % (nombre, str(exc)[:80]), flush=True)
            res = []
        if res:
            for item in res:
                item["backend"] = nombre
            return res
    print("[web] sin resultados en ningún backend", flush=True)
    return []


def fetch_page(url: str, timeout: int = 20, cap: int = 6000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html_mod.unescape(re.sub(r"[ \t\r\f\v]+", " ", text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()[:cap]


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


def run_tool(call: dict, root=None, permitidas=None) -> str:

    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return json.dumps({"error": "argumentos no son JSON"})
    # --- alias: gpt-oss lleva web_search/fetch_page incorporadas y las llamará
    # aunque no se le ofrezcan. Sin herramientas web, "buscar" = buscar en disco.
    if root is not None and permitidas is not None and "web_search" not in permitidas:
        nombre_tool = fn.get("name")
        if nombre_tool == "web_search":
          try:
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
          except Exception as exc:
            return json.dumps({"error": str(exc)[:200]}, ensure_ascii=False)
        if nombre_tool == "fetch_page":
          try:
            u = (args.get("url") or "").strip()
            rel = u
            try:
                rel = str(Path(u).resolve().relative_to(root.resolve()))
            except Exception:
                pass
            print('\n[doc] el modelo pide abrir "%s" -> lo leo del disco' % u[:60], flush=True)
            return json.dumps(fs_read(root, rel), ensure_ascii=False)
          except Exception as exc:
            return json.dumps({"error": str(exc)[:200]}, ensure_ascii=False)
    if permitidas is not None and fn.get("name") not in permitidas:
        print("\n[tool] RECHAZADA '%s' (disponibles: %s)"
              % (fn.get("name"), ", ".join(sorted(permitidas))), flush=True)
        lista = ""
        if root is not None:
            try:
                lista = ("\nDocumentos disponibles ahora mismo: "
                         + ", ".join(d["path"] for d in fs_list(root)))
            except Exception:
                pass
        return ("No existe la herramienta '%s' en esta sesion. Las unicas disponibles son: %s."
                "%s\nUsala ahora." % (fn.get("name"), ", ".join(sorted(permitidas)), lista))
    try:
        if fn.get("name") == "list_docs":
            if root is None:
                return json.dumps({"error": "sin raiz configurada"})
            docs = fs_list(root, args.get("subdir", ""))
            print("\n[doc] lista: %d documentos" % len(docs), flush=True)
            return json.dumps(docs, ensure_ascii=False)
        if fn.get("name") == "read_doc":
            if root is None:
                return json.dumps({"error": "sin raiz configurada"})
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
            print("[web] abre: %s" % u, flush=True)
            try:
                return fetch_page(u)
            except Exception as exc:
                reales = _ultimas_urls()
                print("[web] fallo al abrir; devuelvo %d URLs reales" % len(reales), flush=True)
                return json.dumps({"error": "no se pudo abrir %s (%s)" % (u, str(exc)[:120]),
                                   "urls_validas_de_la_ultima_busqueda": reales,
                                   "instruccion": "esa URL no existe; abre una de estas"},
                                  ensure_ascii=False)
        return json.dumps({"error": "herramienta desconocida"})
    except Exception as exc:
        return json.dumps({"error": str(exc)[:200]})


def http_json(url: str, payload: dict, key: str | None, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %s: %s" % (exc.code, exc.read().decode(errors="replace")[:200]))
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError("sin conexión a %s: %s" % (url, exc))


def stream_turn(base: str, model: str, messages: list, key: str | None, max_tokens: int,
                timeout: int, show_reasoning: bool):
    payload = {"model": model, "messages": messages, "stream": True, "temperature": 0.5,
               "max_tokens": max_tokens, "reasoning_effort": "low",
               "stream_options": {"include_usage": True}}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    content, usage, finish = [], {}, None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
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
                if delta.get("reasoning_content") and show_reasoning:
                    sys.stdout.write(delta["reasoning_content"])
                    sys.stdout.flush()
                if delta.get("content"):
                    content.append(delta["content"])
                    sys.stdout.write(delta["content"])
                    sys.stdout.flush()
    return "".join(content), usage, finish


def tool_loop(base: str, model: str, messages: list, key: str | None, max_tokens: int,
              timeout: int, rounds: int = 4, tools=None, root=None):
    usage_total = {}
    for ronda in range(rounds + 1):
        ultima = ronda == rounds
        if ultima:
            # última ronda: se le prohíbe buscar para que conteste con lo que tiene
            messages = messages + [{"role": "user", "content":
                "Ya no busques más. Responde ahora con la información que hayas obtenido."}]
        payload = {"model": model, "messages": messages, "temperature": 0.3,
                   "max_tokens": max_tokens, "reasoning_effort": "low",
                   "tools": WEB_TOOLS, "tool_choice": "none" if ultima else "auto"}
        resp = http_json(base.rstrip("/") + "/chat/completions", payload, key, timeout)
        for k, v in (resp.get("usage") or {}).items():
            if isinstance(v, int):
                usage_total[k] = usage_total.get(k, 0) + v
        msg = ((resp.get("choices") or [{}])[0].get("message")) or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            texto = msg.get("content") or ""
            if not texto.strip():
                fin = (resp.get("choices") or [{}])[0].get("finish_reason")
                razon = len(msg.get("reasoning_content") or "")
                print("\n[aviso] respuesta sin texto (finish=%s, razonamiento=%d chars)"
                      % (fin, razon), flush=True)
            return texto, usage_total
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        permitidas = {t["function"]["name"] for t in (tools or WEB_TOOLS)}
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": run_tool(call, root, permitidas)})
    return "(se agotaron las rondas de búsqueda)", usage_total


def extract_file(path: Path, cap: int = 20000) -> str:
    ext = path.suffix.lower()
    if ext in {".txt", ".md", ".csv", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")[:cap]
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)[:cap]
    if ext == ".docx":
        import zipfile
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))[:cap]
    if ext == ".eml":
        import email
        from email import policy
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=policy.default)
        head = "\n".join("%s: %s" % (k, msg.get(k, "")) for k in ("From", "To", "Subject", "Date"))
        return (head + "\n\n" + str(msg.get_body(preferencelist=("plain",)).get_content()
                                      if msg.get_body(preferencelist=("plain",)) else ""))[:cap]
    return ""


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def fs_list(root: Path, subdir: str = "", cap: int = 40) -> list:
    base = (root / subdir).resolve() if subdir else root.resolve()
    if not _inside(root.resolve(), base) or not base.is_dir():
        raise ValueError("ruta fuera de la raiz o inexistente")
    out = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in DOC_EXT:
            out.append({"path": str(p.relative_to(root.resolve())),
                        "kb": round(p.stat().st_size / 1024)})
            if len(out) >= cap:
                break
    return out


def fs_read(root: Path, rel: str) -> dict:
    p = (root / rel).resolve()
    if not _inside(root.resolve(), p):
        raise ValueError("ruta fuera de la raiz permitida")
    if not p.is_file():
        raise ValueError("no existe: %s" % rel)
    if p.suffix.lower() not in DOC_EXT:
        raise ValueError("formato no soportado")
    try:
        text = extract_file(p)
    except Exception as exc:
        return {"path": rel, "status": "error_extraccion", "nota": str(exc)[:120]}
    if len(text.strip()) < 40:
        return {"path": rel, "status": "pendiente_ocr", "chars": len(text)}
    return {"path": rel, "status": "ok", "chars": len(text), "text": text}


def load_context(path: str, cap: int = 40000) -> str:
    """Extrae texto de documentos del host. Usa pypdf si está; si no, avisa."""
    base = Path(path).expanduser()
    if not base.exists():
        raise SystemExit("no existe: %s" % base)
    files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    blocks, total, skipped = [], 0, 0
    for f in files:
        ext = f.suffix.lower()
        text = ""
        try:
            if ext in {".txt", ".md", ".csv", ".json"}:
                text = f.read_text(encoding="utf-8", errors="replace")
            elif ext == ".pdf":
                from pypdf import PdfReader          # opcional
                text = "\n".join((p.extract_text() or "") for p in PdfReader(str(f)).pages)
            elif ext == ".docx":
                import zipfile
                with zipfile.ZipFile(f) as zf:
                    xml = zf.read("word/document.xml").decode("utf-8", "replace")
                text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
        except Exception:
            skipped += 1
            continue
        if len(text.strip()) < 40 or total + len(text) > cap:
            skipped += 1
            continue
        blocks.append("### %s\n%s" % (f.name, text))
        total += len(text)
    if not blocks:
        raise SystemExit("no se pudo extraer texto de %s (%d omitidos)" % (path, skipped))
    cab = ("## DOCUMENTOS CARGADOS EN EL HOST\nRuta: `%s`\nFicheros: %s\n"
           "REGLA: el texto está abajo; no pidas que te lo peguen.\n"
           % (path, ", ".join(b.split("\n")[0][4:] for b in blocks)))
    print("contexto: %d documentos (%d caracteres), %d omitidos" % (len(blocks), total, skipped))
    return cab + "\n\n".join(blocks)


HELP = """comandos:
  /salir            termina
  /reset            olvida la conversación
  /estado           base, modelo, herramientas y turnos
  /razonamiento     muestra u oculta el canal de razonamiento
  /docs             lista los documentos de --root
  /leer <ruta>      carga ese documento en el contexto
"""


def main(argv=None) -> int:
    _out()
    p = argparse.ArgumentParser(prog="agent_driver")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    p.add_argument("--model", default="MODELO-EJEMPLO")
    p.add_argument("--api-key-file", default=str(DEFAULT_KEY))
    p.add_argument("--system")
    p.add_argument("--prompt")
    p.add_argument("--context")
    p.add_argument("--root", help="raiz de documentos que el modelo puede leer bajo demanda")
    p.add_argument("--fuentes", default=None,
                   help="JSON con fuentes declaradas (por defecto datos/fuentes.json)")
    p.add_argument("--tools", default="none",
                   help="herramientas del modelo: web, docs o web,docs (por defecto ninguna)")
    p.add_argument("--web-rounds", type=int, default=4)
    p.add_argument("--max-tokens", type=int, default=2048)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--show-reasoning", action="store_true")
    args = p.parse_args(argv)

    key = None
    kf = Path(args.api_key_file).expanduser()
    if kf.exists():
        key = kf.read_text(encoding="utf-8").strip() or None

    system = args.system
    if args.context:
        system = ((system + "\n\n") if system else "") + load_context(args.context)
    global FUENTES_PATH
    FUENTES_PATH = args.fuentes
    _t = [x.strip() for x in (args.tools or "none").split(",") if x.strip()]
    usa_web, usa_docs_tool = "web" in _t, "docs" in _t
    if usa_web:
        system = ((system + "\n\n") if system else "") + (
            "Busca en internet cuando la pregunta pida algo del mundo exterior. La busqueda ya "
            "te devuelve leidos los primeros resultados: usa ese texto y cita la URL. No "
            "inventes URLs ni cifras; si no aparece, dilo.")
        system = ((system + "\n\n") if system else "") + (
            "Tienes herramientas de búsqueda web. Úsalas siempre que la respuesta dependa de "
            "datos actuales (precios, tarifas, condiciones, noticias) y cita las URLs.")
    root = Path(args.root).expanduser().resolve() if args.root else None
    if root is not None and not root.is_dir():
        raise SystemExit("--root no es un directorio: %s" % root)
    if root is not None:
        system = ((system + "\n\n") if system else "") + (
            "El usuario carga documentos con /leer; el texto queda DENTRO de la conversacion, "
            "en un mensaje con la cabecera '## DOCUMENTOS YA CARGADOS'.\n"
            "REGLA OBLIGATORIA: si pregunta por esos documentos, por una carpeta o por una ruta, "
            "responde con el contenido ya cargado. NUNCA contestes que no tienes acceso al "
            "sistema de ficheros, ni pidas que se peguen o exporten los datos: ya estan en el "
            "contexto y basta con leerlos de ahi.")
    if root is not None and usa_docs_tool:
        system = ((system + "\n\n") if system else "") + (
            "Tienes acceso de lectura a los documentos bajo `%s`.\n"
            "HERRAMIENTAS DISPONIBLES EN ESTA SESION: list_docs (sin argumentos o con "
            "subdir) y read_doc (con path relativo).\n"
            "NO EXISTE ninguna herramienta de busqueda web en esta sesion; no llames a "
            "web_search ni a tools parecidas.\n"
            "Flujo obligatorio: 1) list_docs para ver que hay, 2) read_doc del documento "
            "concreto, 3) responder citando el fichero. Nunca digas que no puedes acceder "
            "a ficheros: puedes, con esas dos herramientas." % root)
    herramientas = (FS_TOOLS if (root and usa_docs_tool) else []) + (WEB_TOOLS if usa_web else [])
    messages = []
    if system:
        messages.append({"role": "system", "content": system})

    cargados: dict = {}

    def turn(prompt: str) -> str:
        messages.append({"role": "user", "content": prompt})
        t0 = time.time()
        if herramientas:
            text, usage = tool_loop(args.base_url, args.model, messages, key,
                                    args.max_tokens, args.timeout, args.web_rounds,
                                    tools=herramientas, root=root)
            print(text)
        else:
            text, usage, finish = stream_turn(args.base_url, args.model, messages, key,
                                              args.max_tokens, args.timeout, args.show_reasoning)
            if not text.strip():
                print("\n(modelo sin texto; finish_reason=%s)" % finish)
        if cargados and REFUSAL_RE.search(text or ""):
            print("\n[reintento: nego acceso teniendo los documentos cargados]")
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content":
                "El texto ya esta en tu contexto, en '## DOCUMENTOS YA CARGADOS' (%s). No "
                "necesitas acceso al sistema de archivos ni que nadie pegue datos. Responde "
                "AHORA usando ese texto, citando el fichero." % ", ".join(cargados)})
            text, usage, finish = stream_turn(args.base_url, args.model, messages, key,
                                              args.max_tokens, args.timeout, args.show_reasoning)
            messages.pop()
        messages.append({"role": "assistant", "content": text})
        print("\n[%0.1f s · %s tok]\n" % (time.time() - t0, usage.get("completion_tokens", "?")))
        return text

    host = sys.platform
    if args.prompt:
        turn(args.prompt)
        return 0

    print("agent_driver · %s · %s · host=%s · tools=%s%s"
          % (args.model, args.base_url, host, args.tools,
             (" · root=" + str(root)) if root else ""))
    print("escribe /salir para terminar, /estado para ver el contexto\n")
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
            if cmd == "/reset":
                messages = [m for m in messages if m["role"] == "system"]
                print("(conversación olvidada)")
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
                        if not ruta.is_absolute() and root is not None:
                            ruta = root / pedazo
                        texto = extract_file(ruta)
                        if len(texto.strip()) < 40:
                            print("(sin capa de texto: necesitaria OCR)")
                            continue
                        clave = str(ruta)
                        if clave in cargados:
                            print("(ya estaba cargado %s: %d caracteres)" % (ruta.name, cargados[clave]))
                            continue
                        messages.append({"role": "user", "content":
                                         "## DOCUMENTOS YA CARGADOS\n"
                                         "El texto siguiente YA esta en esta conversacion: no hace falta "
                                         "acceso al sistema de ficheros ni que se pegue nada. Es la fuente.\n\n"
                                         "### %s (%d caracteres)\n%s" % (ruta.name, len(texto), texto)})
                        cargados[clave] = len(texto)
                        print("(cargado %s: %d caracteres · total en contexto %d)"
                              % (ruta.name, len(texto), sum(cargados.values())))
                    except Exception as exc:
                        print("(error leyendo %s: %s)" % (pedazo, str(exc)[:120]))
            elif cmd == "/docs":
                if root is None:
                    print("(no hay --root)")
                    continue
                try:
                    for x in fs_list(root):
                        print("  %-50s %6d KB" % (x["path"], x["kb"]))
                except Exception as exc:
                    print("(error: %s)" % str(exc)[:120])
            elif cmd == "/estado":
                print("modelo=%s base=%s tools=%s turnos=%d mensajes=%d"
                      % (args.model, args.base_url, args.tools,
                         sum(1 for m in messages if m["role"] == "user"), len(messages)))
            elif cmd == "/razonamiento":
                args.show_reasoning = not args.show_reasoning
                print("(razonamiento %s)" % ("visible" if args.show_reasoning else "oculto"))
            else:
                print(HELP)
            continue
        try:
            turn(line)
        except RuntimeError as exc:
            print("\n[error] %s\n" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
