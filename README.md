# modelgate · enrutado de trabajo entre modelo local y cloud

CLI y librería **independiente de cualquier proyecto**: decide a qué modelo va
cada trabajo y lo ejecuta. Vive en `~/Repos/modelgate`; los proyectos la
referencian por ruta, no la copian (mismo contrato que `net-conn`).

- `modelgate.py` — CLI stdlib: perfiles, política, túnel SSH, ejecución.
- `models.json` — perfiles reales (no se versiona). `models.json.example` es la plantilla.
- `tests/` — pruebas con un servidor OpenAI-compatible de mentira, sin red externa.

## Regla de seguridad

Un trabajo marcado `--sensitivity local-only` **solo** puede ir a un perfil
marcado `local-only`. Nunca hay caída automática a la nube: si no existe perfil
local, `route` falla con código 3. Es el punto que hace que «todo local» sea una
propiedad del sistema y no una intención.

## Uso

```bash
cp models.json.example models.json        # ajusta host, modelo y ruta de la clave

python3 modelgate.py list
python3 modelgate.py status               # ¿responde cada destino?
python3 modelgate.py tunnel up local      # túnel SSH 127.0.0.1:18080 -> equipo:8080
python3 modelgate.py run local --prompt "Extrae los campos de esta factura: ..."
python3 modelgate.py run local --file factura.txt --sensitivity local-only --json
python3 modelgate.py route --sensitivity local-only --file factura.txt

# documentos (delega en ~/Repos/docpipe)
python3 modelgate.py docs ~/Documents --kind factura --dry-run
python3 modelgate.py docs ~/Documents --kind factura --out ./_analisis
python3 modelgate.py docs 'C:\\Users\\usuario\\Documents' --source equipo
```

Códigos de salida: `0` correcto, `1` error genérico, `2` configuración,
`3` política (destino no admite la sensibilidad), `4` destino inaccesible.
Los scripts que la encadenan dependen de esto: no cambiarlo.

## Ciclo de vida: el modelo no está siempre encendido

Perdigon se queda apagado y sin modelo en VRAM hasta que hace falta. Cada comando
(`chat`, `docs`, `run`) hace el ciclo completo, y `model` lo controla a mano:

```bash
modelgate model status        # ¿host arriba? ¿modelo escuchando?
modelgate model up            # WoL si hace falta + arrancar el modelo
modelgate model down          # parar el modelo (libera ~11 GB de VRAM)
modelgate model off           # parar y apagar el equipo
modelgate chat --keep-alive   # no parar nada al terminar
```

Ciclo medido (equipo apagado → respuesta → apagado): **89 s**.

1. Si el host no responde al SSH → **Wake-on-LAN** y espera (37 s medidos).
2. Si el puerto 8080 no escucha → `schtasks /run llama-server` y espera a `/health`.
3. Al terminar: si lo arrancamos nosotros, `schtasks /end` (con remate del proceso
   si sobrevive) → VRAM liberada.
4. Si además lo despertamos nosotros y el comando fue `chat` → `shutdown /s`.

Todo se configura en el perfil:

```json
"lifecycle": {"auto_stop": true, "task": "llama-server",
              "wake": true, "shutdown": "if_woken"}
```

`shutdown`: `if_woken` (por defecto: solo apaga si lo encendimos nosotros),
`always` o `never`. La tarea `llama-server` ya **no tiene disparador**: no arranca
al iniciar sesión, solo cuando `modelgate` la invoca.

## Interacción con el modelo: tres niveles

`modelgate` no es la única puerta, es la capa de política. Para hablar con el
modelo local hay tres formas, de más ligera a más completa:

| Nivel | Comando | Qué es | Cuándo |
|---|---|---|---|
| Directo | `modelgate chat` | REPL con streaming, historial en RAM, sin herramientas | conversar, pensar, probar prompts |
| Agente | `hermes -m localdocs chat` | Hermes completo: herramientas, skills, ficheros | trabajar con el agente contra el modelo local |
| Web | túnel arriba + `http://127.0.0.1:18080/` | la UI web que sirve `llama-server` | ver el modelo sin terminal (pide la API key) |
| Lote | `modelgate docs` / `run` | extracción y consultas sin interacción | procesar documentos |

### `modelgate chat`

```bash
modelgate chat                      # sesión interactiva contra el perfil local
modelgate chat --prompt "resume esto: ..."   # un turno y sale (scripts)
```

`/ayuda` dentro de la sesión: `/reset`, `/sistema`, `/razonamiento` (ver el canal
de razonamiento de gpt-oss), `/guardar <ruta>` (transcripción a 0600), `/estado`.

Por defecto el historial vive **solo en memoria**: si no hay `/guardar`, no queda
nada en disco. Si el modelo devuelve razonamiento sin texto, lo dice en vez de
mostrar una respuesta vacía.

### `--context`: documentos dentro de la conversación

`chat` **no tiene herramientas**: no puede abrir ficheros por su cuenta. Para
trabajar sobre una carpeta se le pasa el texto ya extraído:

```bash
modelgate chat --context ~/Documents/facturas
modelgate chat --context ~/Documents/facturas --context-max-docs 30 --context-max-chars 60000
```

Al arrancar, `docpipe` extrae el texto (PDF con capa de texto, docx, eml, txt) y
lo inyecta como contexto, con el nombre de cada fichero para que el modelo pueda
citarlo. Los escaneados se omiten y se dice cuántos. Presupuesto por defecto:
20 documentos y 40.000 caracteres.

Lo que **no** hace: abrir ficheros nuevos a mitad de conversación. Para eso está
el agente completo (`hermes -m localdocs chat`), que sí tiene herramientas.

### `--root`: elegir el documento dentro de la sesión

`--root` habilita dos comandos deterministas, sin depender de que el modelo sepa
usar herramientas:

```bash
modelgate chat --root ~/Documents/facturas
```

```
tú> /docs                                  lista los documentos de la carpeta
tú> /leer factura_FE2600999646.pdf         carga ese documento en el contexto
tú> ¿qué total tiene y de qué periodo es?
```

`/leer` acepta uno o varios ficheros (`/leer a.pdf b.pdf`), nombre relativo a
`--root` (contenido dentro de esa carpeta) o ruta absoluta tecleada por ti, que
puede estar fuera de `--root`. Funciona con PDF, docx, eml, txt y md; los escaneados
avisan de que necesitan OCR. Repetir un fichero no duplica el contexto: avisa de
que ya estaba cargado. Cada carga muestra el total acumulado.

Si el modelo contesta igualmente que «no tiene acceso a ficheros» (gpt-oss tiende
a ello cuando la pregunta menciona una ruta), modelgate reintenta una vez
recordándole que el texto ya está en el contexto; en las pruebas recupera la
respuesta correcta en el segundo intento.

`--tools docs` (opt-in) añade además que el **modelo** pueda llamar a
`list_docs`/`read_doc` por su cuenta. Con MODELO-EJEMPLO no es fiable —se engancha a
su herramienta `web_search` incorporada— así que el camino recomendado es
`/docs` + `/leer`.

### Receta: analizar facturas y comparar tarifas

Con `--tools web`, el modelo busca por su cuenta y **se le entregan leídas las dos
primeras páginas** del resultado: no hay que copiarle precios ni darle URLs.

```
$ modelgate chat --root ~/Documents/facturas --tools web --max-tokens 1500
tú> /leer factura_A.pdf factura_B.pdf
tú> Analiza estas facturas, busca las tarifas vigentes de Som Energia para 2.0TD
    y dime si conviene cambiar, con cifras y citando la URL.
```

Por qué se le entrega leído: MODELO-EJEMPLO **ignora las URLs** que devuelve la
búsqueda y se inventa rutas (`/es`, `/tarifas`), y sin texto delante rellena los
huecos con tarifas inexistentes. Con el texto de la página en el turno de la
herramienta acierta las cifras y cita la fuente.

Si quieres fijar una fuente concreta, `datos/fuentes.json` + `fetch_source`
siguen disponibles, pero ya no hacen falta.

<details><summary>Receta anterior (aportando la tabla de precios)</summary>

El modelo solo es fiable con lo que tiene delante. Para una comparativa de tarifa,
cárgale **las facturas y los precios oficiales**, y prohíbele recordar de memoria:

```
$ modelgate chat --root ~/Documents/facturas --keep-alive --max-tokens 1500
tú> /leer factura_A.pdf factura_B.pdf ~/Repos/modelgate/datos/tarifas_somenergia-2026-10.txt
tú> Con los kWh de Punta, Llano y Valle de cada factura, y SOLO los precios del
    documento de tarifas, calcula cuánto costaría la energía con esa tarifa,
    compara con lo que se pagó y dime si conviene cambiar. Si falta un dato, dilo;
    no inventes tarifas ni precios.
```

Acierta la aritmética y admite cuándo falta un dato. Los precios son públicos y
caducan: el fichero va fechado.

</details>

### `--tools web`: buscar en internet

```bash
modelgate chat --tools web --prompt "¿qué tarifas domésticas ofrece Som Energia?"
modelgate chat --context ~/Documents/facturas --tools web   # comparar facturas vs tarifas
```

El modelo pide búsquedas con `tool_calls` (llama.cpp ya las soporta con `--jinja`)
y `modelgate` las ejecuta: `web_search` sobre DuckDuckGo HTML (sin API key) y
`fetch_page` para leer una URL concreta. Máximo 4 rondas (`--web-rounds`).

### Modo delegado: `--source equipo`

```bash
modelgate chat --source equipo --tools web
```

Ahí **todo ocurre en el host**: la inferencia, la decisión de buscar y la búsqueda
misma. El Mac solo mantiene el SSH y pinta la terminal; su proceso no abre ni una
conexión a internet. Verificado: el árbol de procesos del cliente tiene una única
conexión, `→ equipo:22`, y cero al 443.

El driver vive en el host (`C:\Users\<u>\tools\agent\agent_driver.py`), sin
dependencias externas. Se despliega con:

```bash
scp -i ~/.ssh/equipo agent_driver.py \
  usuario2@example.com:C:/Users/usuario/tools/agent/agent_driver.py
```

Consecuencias: `--context` exige rutas **del host** (los documentos del Mac van con
`--source local`), y la clave del modelo no sale de allí.

**Nota de red comprobada:** DuckDuckGo responde `HTTP 202` (página de desafío) a
Perdigon y `200` al Mac. El driver conmuta a **Bing** cuando DDG no devuelve
resultados, y desenvuelve sus URLs (`bing.com/ck/a?u=a1<base64>`).

**Esto rompe el aislamiento, y es a propósito:** cada consulta sale del Mac hacia
DuckDuckGo. Por eso `--tools web` es explícito y **cada búsqueda se imprime antes
de enviarse** (`[web] busca: "..."`), para que veas exactamente qué sale. Si
combinas `--context` con `--tools web`, el modelo puede meter datos de tus
documentos en la consulta: ese es el riesgo real, no el modelo.

Verificado: precios de Som Energia (0,194 / 0,124 / 0,099 €/kWh y 29,934 €/kW
año) localizados en la página oficial. Pero el modelo **mezcla filas** al resumir
una tabla: sirve para orientarse, no como dato contable sin cotejar.

**El encuadre importa.** Con el contexto cargado, el modelo puede negarse igual
(«no tengo acceso a tus ficheros») si la pregunta menciona una ruta o pide algo
que no está en los documentos. Por eso el bloque de contexto lleva la ruta, la
lista de ficheros y una regla explícita de no negar el acceso, y si aun así
niega, `chat` **reintenta una vez** con una corrección. Verificado con facturas
reales: sin el encuadre, negativa; con él, respuesta citando el fichero.

## Esquema de `models.json`

```json
{
  "default_profile": "local",
  "profiles": {
    "local": {
      "kind": "openai",
      "transport": "ssh-tunnel",
      "host": "equipo",
      "remote_host": "127.0.0.1",
      "remote_port": 8080,
      "local_port": 18080,
      "model": "MODELO-EJEMPLO",
      "sensitivity": "local-only",
      "api_key": {"source": "ssh-file", "host": "equipo",
                  "path": "C:\\Users\\<usuario>\\.llama\\api.key"},
      "defaults": {"temperature": 0.1, "max_tokens": 1024}
    },
    "cloud": {"kind": "hermes", "provider": "opencode-go",
              "model": "deepseek-v4.1-flash", "sensitivity": "cloud-ok"}
  }
}
```

- `kind`: `openai` (endpoint compatible) o `hermes` (delega en `hermes -z`, usa la
  suscripción ya configurada sin duplicar credenciales).
- `transport`: `http` (URL directa) o `ssh-tunnel` (recomendado: el endpoint del
  modelo escucha en loopback del host remoto y solo se alcanza por SSH).
- `api_key.source`: `none`, `env` (nombre de variable) o `ssh-file` (lee la clave
  del host remoto en cada ejecución; **no se guarda en local**).
- `host` se resuelve contra el inventario de `net-conn`; no hay contraseñas.

`hermes` puede sobreescribirse con `MODELGATE_HERMES` (ruta del binario).

## Auditoría

Cada ejecución deja una línea en `~/.modelgate/audit.jsonl` (modo 0600): perfil,
modelo, sensibilidad, `sha256` del prompt, caracteres, tokens y milisegundos.
**No se guarda el contenido** del documento.

## Estado verificado (2026-11-01)

| Comprobación | Resultado |
|---|---|
| Unit tests | 20 casos en verde |
| Endpoint local | `MODELO-EJEMPLO` en `127.0.0.1:8080` (loopback), 64K de contexto |
| Sin clave | `HTTP 401` |
| Velocidad @16K | 184 tok/s prompt, 122 tok/s generación |
| Velocidad @64K | 86 tok/s prompt, 30 tok/s generación (KV en f16) |
| Hermes → local | alias `localdocs`: respondió `local-ok` en 36 s |
| Superficie LAN | ninguna: sin reglas de firewall, solo túnel SSH |
| Arranque desatendido | auto-login: SSH a los 54 s del reinicio, `llama-server` solo |
| Riesgo abierto | BitLocker con `ProtectionStatus=Off` (sin protector TPM) |

## Contexto: 64K es obligatorio para Hermes

Hermes Agent rechaza modelos con menos de 64 000 tokens de ventana. El servidor
debe arrancar con `-c 65536 --parallel 1`. Además Hermes auto-detecta mal el
contexto de un endpoint `custom`, así que hay que fijarlo explícitamente:

```bash
hermes config set model_overrides.custom.gpt-oss-20b.context_window 65536
```

Sin esa clave el error es `Model MODELO-EJEMPLO has a context window of 16,384
tokens, which is below the minimum 64,000` aunque el servidor sirva 64K.

## Perfil de Hermes para documentos

```bash
hermes config set model_aliases.localdocs.provider custom
hermes config set model_aliases.localdocs.model MODELO-EJEMPLO
hermes config set model_aliases.localdocs.base_url http://127.0.0.1:18080/v1
hermes config set model_aliases.localdocs.key_env LLAMA_LOCAL_API_KEY
# la clave va en ~/.hermes/.env (0600), nunca en config.yaml
```

Uso: `hermes -m localdocs chat` (sesión local) o `modelgate run local --file ...`
(scriptable). El alias exige el túnel arriba: `modelgate tunnel up local`.

## Límites conocidos

- El túnel es un proceso `ssh -N` con pidfile; si el Mac se suspende, el pid
  puede quedar vivo pero sin forward. `modelgate tunnel up` lo revalida.
- GPT-OSS-20B razona antes de responder: usa `reasoning_effort` bajo o
  `max_tokens` amplio, o el contenido saldrá truncado.
- No hay reintentos ni streaming todavía.
