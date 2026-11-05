# DECISIONS.md — modelgate y el runtime local

Más reciente arriba. Una decisión se **supera** con una entrada nueva, no se
reescribe. Fechas reales de sesión de trabajo.

---

## 2026-11-01 · Documentos en la sesión: comando del usuario, no herramienta del modelo

**Contexto.** Se intentó que el modelo eligiera y leyera ficheros con herramientas
(`list_docs`/`read_doc`). Con MODELO-EJEMPLO no funciona: **trae `web_search` y
`fetch_page` incorporadas** en su formato harmony y las llama aunque no se le
ofrezcan; rechazarlas produjo 4 llamadas seguidas y una respuesta vacía, y la
intercepción (traducir `web_search` a búsqueda en disco) lo dejó dando vueltas
sin contestar. Igual en Mac y en Windows, así que no era la plataforma.

**Decisión.** El usuario decide qué documento entra: `--root <carpeta>` habilita
`/docs` (lista) y `/leer <ruta>` (carga el texto en la conversación). Las
herramientas del modelo quedan como opt-in (`--tools docs`) y documentadas como
poco fiables con este modelo.

**Alternativas descartadas.** Insistir en el bucle de herramientas: depende de un
comportamiento del modelo que no controlamos. Precargar todo con `--context`: no es
selectivo y gasta contexto.

**Consecuencias.** Flujo determinista: el modelo no puede abrir nada que el usuario
no haya cargado, y `/leer` es una acción explícita y visible. Verificado en local
(42,79 € y periodo leídos de una factura real) y delegado (CIF y total del PDF en el
host, 0,8 s).

---

## 2026-11-01 · `/leer` acumula documentos y reintenta la negativa

**Contexto.** Con un documento cargado, una pregunta que mencionaba la ruta
(«analiza las facturas en /Users/...») hacía que el modelo contestara que no podía
acceder al sistema de ficheros, ignorando el texto ya cargado. El reintento
existente solo se activaba con `--context`.

**Decisión.** Tres cambios: (1) `/leer a.pdf b.pdf` carga varios; (2) repetir un
fichero no duplica el contexto, avisa y sigue; (3) el reintento por negativa se
activa también con documentos cargados por `/leer`, y el recordatorio nombra el
bloque `## DOCUMENTOS YA CARGADOS` y los ficheros concretos.

**Consecuencias.** Verificado con las dos facturas reales (283 y 493 kWh; 42,79 y
66,49 €) en 2,8 s tras el reintento, y en delegado con CIF y total en 0,5 s.

---

## 2026-11-01 · La búsqueda entrega el texto leído (el modelo ignora las URLs)

**Contexto.** Con `--tools web` el modelo buscaba y luego abría URLs inventadas
(`/es`, `/tarifas`) en vez de las que devolvía la búsqueda, así que se quedaba sin
datos y rellenaba con tarifas inexistentes. Diagnóstico inicial equivocado: se
culpó a JavaScript y a los buscadores. Medido después: DuckDuckGo responde 200
desde el host y su **primer resultado es la página de tarifas correcta**; Bing sí
devuelve basura (descarta términos de la consulta) y por eso el failover empeoraba
las cosas. El fallo era de comportamiento del modelo, no de red.

**Decisión.** `web_search` devuelve, además de la lista, el texto ya extraído de
los dos primeros resultados (`ya_leidos`). El contenido no se puede ignorar como se
ignoran las URLs. `fetch_page` sigue disponible, y si el modelo abre una URL
inexistente se le devuelven las URLs reales de la última búsqueda.

**Consecuencias.** Flujo autocontenido verificado en delegado: una búsqueda, dos
páginas leídas, cinco precios correctos y URL citada en 6,1 s; y con una factura
delante, detecta que le faltan kWh en vez de inventarlos. Se descarta la idea de
declarar fuentes URL a URL: añade trabajo manual sin resolver el problema real.

---

## 2026-11-01 · Comparar tarifas: la tabla dentro, no la memoria del modelo

**Contexto.** Se probó a que el modelo buscara las tarifas de Som Energia por su
cuenta (`--tools web`). Hizo 4 búsquedas y respondió **inventando** tarifas
inexistentes (1.0TD, 5.0TD, 6.0TD) y un «factor 29/365» que no significa nada. La
página real de tarifas
(`/es/tarifas-luz/domestico-periodos`) carga los precios por JavaScript, así que
`fetch_page` (texto plano) no los ve y el modelo se queda sin datos: o alucina, o
se rinde.

**Decisión.** Los precios oficiales se guardan como dato público en
`datos/tarifas_somenergia-YYYY-MM.txt` y se cargan con `/leer` junto a las
facturas. El prompt impone «usa solo estas cifras; no inventes tarifas». Para
obtenerlos hay que renderizar la página (navegador), no basta el HTML.

**Consecuencias.** Verificado: con la tabla delante acierta la aritmética al
céntimo (31,723 kWh × 0,249 = 7,90 €; total 23,77 € frente a 20,57 € pagados) y
declara cuándo falta un dato en vez de rellenarlo. Sigue haciendo falta
`--max-tokens` alto (1500) para dos facturas: con 800 se corta a media tabla.

---

## 2026-11-01 · Bajo demanda: WoL al entrar, apagado al salir

**Contexto.** El modelo ocupaba ~11 GB de VRAM permanentemente porque la tarea se
lanzaba al iniciar sesión, para un uso esporádico.

**Decisión.** La tarea `llama-server` deja de tener disparador: no arranca sola. Cada
comando de `modelgate` despierta el host por Wake-on-LAN si está apagado, arranca
el modelo, y al terminar lo para; si lo despertamos nosotros y el comando fue
`chat`, apaga el equipo. Todo configurable en `lifecycle` del perfil.

**Alternativas descartadas.** Solo parar el modelo y dejar el equipo encendido:
ahorra VRAM pero no consumo. Apagar siempre: mataría una sesión que el usuario abrió
a mano. `--keep-alive` cubre el caso de estar iterando.

**Consecuencias.** Latencia de arranque real: **89 s** de punta a punta con el equipo
apagado (37 s de boot + 15-19 s de modelo + respuesta + 27 s de apagado). El coste se
paga una vez por sesión, no por turno.

---

## 2026-11-01 · Dos trampas del Programador de tareas al pasar a bajo demanda

**Contexto.** Para que no arrancara sola se probó `Disable-ScheduledTask`, y después
`Set-ScheduledTask -Trigger $null`.

**Decisión.** (1) Una tarea **deshabilitada no se puede lanzar con `schtasks /run`**
(error explícito), así que hay que dejarla habilitada y **vaciar los disparadores**:
`$t.Triggers = @()` y `Register-ScheduledTask -InputObject $t -Force`. (2)
`schtasks /end` cierra la tarea pero a veces deja el proceso `llama-server` vivo:
después del `end` hay que comprobar el puerto y rematar con `Stop-Process -Force`.

**Consecuencias.** Sin el remate, el modelo seguía ocupando VRAM tras «pararlo» y el
aviso aparecía en el log. Con él, la parada es limpia y verificable por puerto.

---

## 2026-11-01 · El flujo delegado: la actividad entera vive en el host

**Contexto.** Con el bucle de herramientas en el Mac, la decisión era del modelo de
Perdigon pero el socket de la búsqueda lo abría el Mac. El requisito del usuario:
si se usa este flujo, toda la actividad queda delegada al host.

**Decisión.** `--source equipo` ejecuta un driver (`agent_driver.py`, stdlib) en
el host por SSH: allí se infiere (loopback), se decide y se busca. El cliente solo
mantiene la conexión SSH. Verificado por muestreo: el árbol de procesos del Mac
tiene una única conexión a `equipo:22` y ninguna al 443.

**Alternativas descartadas.** Mantener el bucle en el Mac: mismo IP público, pero
el host dejaba de ser el único con salida. Proxy inverso en el host para que el
Mac buscara «a través» de él: más piezas y el contenido seguía pasando por el Mac.

**Consecuencias.** `--context` con `--source equipo` exige rutas del host; mezclar
documentos del Mac con búsqueda delegada no está soportado y se rechaza con error.
El driver se despliega por `scp` y no tiene dependencias (pypdf opcional para PDF).

---

## 2026-11-01 · DuckDuckGo bloquea al host; Bing como respaldo

**Contexto.** Las búsquedas devolvían 0 resultados desde Perdigon aunque desde el
Mac funcionaban. Medido: DDG responde `HTTP 202` con 14 KB de página de desafío al
host y `200` al Mac. No era el parser.

**Decisión.** `web_search` intenta DuckDuckGo y, si no devuelve nada, conmuta a
Bing, cuyos enlaces vienen envueltos en `bing.com/ck/a?u=a1<base64url>` y hay que
desenvolver. Se registra el backend usado en cada resultado.

**Alternativas descartadas.** API de pago (Brave/Tavily): credencial nueva.
Insistir con DDG desde el host: es un muro de bot, no un fallo transitorio.

**Consecuencias.** El resultado depende de dos terceros; si Bing también bloquea,
la herramienta devuelve lista vacía y lo dice, en vez de inventar. La latencia
sube por el intento fallido contra DDG.

---

## 2026-11-01 · Búsqueda web: consentimiento explícito y consulta visible

**Contexto.** El usuario quiere que el modelo consulte tarifas que no están en las
facturas. Sin red, el modelo improvisa con conocimiento desactualizado.

**Decisión.** `--tools web`: bucle de `tool_calls` nativo (llama.cpp los soporta
con `--jinja`) con dos herramientas, `web_search` (DuckDuckGo HTML, sin clave) y
`fetch_page`. Es opt-in, y **cada consulta se imprime antes de salir**
(`[web] busca: "..."`). Sin `--tools`, el chat sigue siendo hermético.

**Alternativas descartadas.** Búsqueda web por defecto: convierte cada pregunta en
una fuga silenciosa. API de pago (Brave/Tavily): otra credencial para algo que
DDG resuelve. `hermes -m localdocs` como única vía: funciona, pero arrastra un
prompt de agente de 64K y ~36 s por turno.

**Consecuencias.** El aislamiento deja de ser total cuando se usa esta bandera, y
eso queda dicho en la salida. Riesgo real: con `--context`, el modelo puede
incluir datos de los documentos en la consulta; el aviso impreso permite verlo y
cortar. El resumen de tablas mezcla filas: se verifican los números contra la URL.

---

## 2026-11-01 · El contexto cargado no basta: encuadre y reintento

**Contexto.** Con `--context`, el modelo seguía contestando «no tengo acceso a
tus archivos» aunque el texto estaba en el mensaje de sistema. Diagnóstico: la
extracción era buena (7.862 y 8.448 caracteres, 75% alfanumérico, con «Som
Energia, SCCL CIF:»), así que el fallo era de encuadre, no de datos: la pregunta
mencionaba una ruta y pedía algo que no está en los documentos (tarifas
comerciales actuales), y el modelo se atrincheró.

**Decisión.** (1) El bloque de sistema lleva la ruta exacta, la lista de ficheros
incluidos y una REGLA OBLIGATORIA de no negar el acceso ni pedir que le peguen
datos. (2) Si con contexto cargado la respuesta casa con un patrón de negativa,
se reintenta una sola vez con un mensaje correctivo explícito.

**Alternativas descartadas.** Solo empeorar el prompt: no cubre las variantes en
que el modelo insiste. Meter herramientas de fichero: convierte `chat` en agente.

**Consecuencias.** Un reintento como máximo (coste: un turno extra de prefill).
Detectar la negativa por patrón es heurístico: si el modelo se niega con otras
palabras, no se reintenta y el usuario lo ve tal cual.

---

## 2026-11-01 · `chat --context`: acceso explícito a documentos, sin herramientas

**Contexto.** Primer uso real: «analiza las facturas en ~/Documents/facturas». El
modelo contestó que no podía acceder a ficheros. Era cierto y era el diseño:
`chat` es un LLM puro, sin herramientas.

**Decisión.** Añadir `--context <path>`: al arrancar, `docpipe extract` vuelca el
texto de la carpeta y se inyecta como contexto con presupuesto (20 documentos,
40.000 caracteres). El modelo cita el fichero en el que se apoya.

**Alternativas descartadas.** Dar un bucle de herramientas al chat (leer ficheros
bajo demanda): más potente, pero convierte `chat` en un agente con acceso al
disco y multiplica los turnos a 30 tok/s. Para eso ya está
`hermes -m localdocs chat`, que trae herramientas de verdad. Inyectar la carpeta
entera sin presupuesto: rompe el contexto de 64K.

**Consecuencias.** El acceso lo decide el usuario con una ruta explícita, no el
modelo. `chat` no abre ficheros nuevos a mitad de conversación: si cambia el
conjunto de documentos, se relanza con otro `--context`.

---

## 2026-11-01 · `modelgate chat`: el enrutado no puede ser una jaula

**Contexto.** Con `run` y `docs`, la única forma de hablar con el modelo local era
pedirle algo cerrado. El usuario lo señaló: no había sesión directa, todo pasaba
por el esquema de `modelgate.py`.

**Decisión.** Añadir `chat`: REPL con streaming SSE, historial en memoria,
comandos mínimos (`/reset`, `/sistema`, `/razonamiento`, `/guardar`, `/estado`) y
un modo `--prompt` para scripts. La política se sigue aplicando (un perfil
`cloud-ok` no acepta trabajo `local-only`), pero la ergonomía deja de ser un
embudo. Para el agente completo se usa `hermes -m localdocs`; la UI web de
`llama-server` queda disponible por el túnel.

**Alternativas descartadas.** Solo `hermes -m localdocs`: es agente, no
conversación; su prompt de sistema hace que el primer turno tarde ~36 s. Un
`llama-cli` en el host: obliga a sesión SSH interactiva y no pasa por la política.

**Consecuencias.** El historial no se escribe en disco salvo `/guardar` explícito
(0600). El razonamiento de gpt-oss se separa del contenido: si el modelo no
devuelve texto, se avisa con `finish_reason` en vez de mostrar una respuesta
vacía, y el turno vacío no se añade al historial.

---

## 2026-11-01 · Los procesos no interactivos no heredan stdin

**Contexto.** `modelgate chat` con la entrada por tubería terminaba en el primer
prompt sin leer nada. Causa: el `ssh -N` del túnel y `ssh_out` de net-conn
heredaban stdin y consumían las líneas del padre.

**Decisión.** `stdin=subprocess.DEVNULL` en el túnel de modelgate y en las tres
llamadas no interactivas de `net-conn` (`ssh_ready`, `remote_os`, `ssh_out`). En
`net-conn`, `cmd_run` sigue heredando stdin a propósito: ahí puede ser útil.

**Consecuencias.** Cualquier REPL que llame a net-conn o levante un túnel funciona
con entrada por tubería y en cron. Es un bug de librería: se corrigió en `net-conn`,
no con un parche en el consumidor.

---

## 2026-11-01 · Ventana de contexto: 64K con un solo slot

**Contexto.** Servido a 16K y 4 slots, Hermes rechazaba el modelo: exige un
mínimo de 64 000 tokens. `MODELO-EJEMPLO` soporta 131K, la duda era la VRAM
(11,3 GiB de pesos en 16 GiB).

**Decisión.** `-c 65536 --parallel 1` con KV en f16. Probado antes en un puerto
aparte: carga y responde, y como el KV se reserva entero al arrancar, si carga
entra para cualquier longitud. Se descartó KV `q8_0`: entraba igual pero no
aportaba velocidad.

**Alternativas descartadas.** 32K: por debajo del mínimo de Hermes. KV q8_0:
menos VRAM, misma velocidad medida. 128K: no cabe.

**Consecuencias.** Generación de 122 tok/s (16K) a 30 tok/s (64K) y prefill de
184 a 86 tok/s: el coste es real y lo paga el agente, donde el prompt es grande.
Una sesión de Hermes contra el modelo local tarda ~36 s en el primer turno.

---

## 2026-11-01 · Hermes apunta al local por alias, no por `model.base_url`

**Contexto.** Cambiar `model.base_url` habría movido *toda* la instalación al
modelo local. Se quería conmutar por sesión.

**Decisión.** Alias `localdocs` (`provider: custom`, `base_url` al túnel,
`key_env: LLAMA_LOCAL_API_KEY`) más `model_overrides.custom.gpt-oss-20b.context_window:
65536`, porque Hermes auto-detecta mal el contexto de endpoints custom y sin ese
pin rechaza el modelo por «16 384 tokens».

**Alternativas descartadas.** `model.context_length` global: habría capado
también el modelo cloud por defecto. Perfil de Hermes aparte: más aislamiento,
pero duplica configuración y credenciales; queda como opción si se quiere
separar del todo.

**Consecuencias.** `hermes -m localdocs` para trabajo local; el resto sigue en
opencode-go. La clave vive en `~/.hermes/.env` (0600) — es el único secreto
copiado al Mac, y solo abre un endpoint de loopback.

---

## 2026-11-01 · Auto-login activado (resuelve la entrada siguiente)

**Contexto.** El host se quedaba en la pantalla de bloqueo y ni `sshd` ni
`llama-server` arrancaban, así que el flujo «WoL → trabajar» exigía presencia
física. La opción de suspender (S3) se descartó porque no cubre un apagado o un
corte de luz.

**Decisión.** Auto-login con Sysinternals **Autologon 3.10** (firmado por
Microsoft) en `C:\Users\usuario\tools\Autologon64.exe`. Verificado en registro:
`AutoAdminLogon=1`, `DefaultUserName=usuario`, `DefaultDomainName=PERDIGÓN`, sin
contraseña en claro (secreto LSA). El usuario introdujo la contraseña él mismo;
nunca pasó por el agente.

**Alternativas descartadas.** `netplwiz` (en 26H2 la casilla puede no aparecer).
Suspender S3: no sobrevive a un corte de luz. `sshd` como servicio más tarea no
interactiva: mismo coste de credenciales y riesgo de reintroducir el fallo de
autenticación del servicio documentado en `net-conn`.

**Consecuencias.** Reinicio real verificado el 2026-11-01: **SSH disponible a los
54 s y `llama-server` cargado solo**, sin tocar el teclado. Riesgo aceptado: la
sesión queda abierta a quien tenga acceso físico. **Pendiente de hardening:**
BitLocker está `FullyEncrypted` con `ProtectionStatus=Off` (solo protector de
recovery password, sin TPM); añadir TPM con
`manage-bde -protectors -add C: -TPM` y `manage-bde -protectors -enable C:`
no rompe el auto-login.

---

## 2026-11-01 · Arranque desatendido: pendiente de decidir

**Contexto.** Tras el reinicio real se comprobó que Perdigon se queda en la
pantalla de bloqueo: sin sesión de usuario, ni `OpenSSH-User` ni `llama-server`
arrancan, y el puerto 22 no abre. El ciclo «WoL → trabajar» solo funciona si
alguien desbloquea la máquina.

**Estado.** Sin decisión. Opciones sobre la mesa: suspender (S3/hibernar) en vez
de apagar para conservar la sesión; activar auto-login (guarda la contraseña en
un secreto LSA, riesgo de acceso físico); o `sshd` como servicio más
`llama-server` con «ejecutar aunque el usuario no haya iniciado sesión» (mismo
coste de credenciales y riesgo de reintroducir el fallo de autenticación del
servicio documentado en `net-conn`).

**Consecuencias mientras no se decida.** Un apagado real exige presencia física.
Las comprobaciones de este repo que tocan Perdigon deben asumir que el host
puede estar en la pantalla de bloqueo y fallar con código 4, no quedarse
colgadas.

---

## 2026-11-01 · Modelo: usar el GPT-OSS-20B MXFP4 ya presente

**Contexto.** El inventario inicial (búsqueda de `*.gguf` con `-Filter`+`-Depth`
en PowerShell 5.1) no encontró modelos y se eligió bajar Qwen2.5-14B Q4_K_M. El
usuario corrigió: existía `C:\Users\usuario\modelos\gpt-oss-20b-MXFP4.gguf`
(12,1 GB). Verificado por listado directo del directorio.

**Decisión.** Servir `MODELO-EJEMPLO` MXFP4 (MoE, 3,6B activos) y no descargar
nada. Plan B si la calidad de extracción no basta: Qwen2.5-14B Q4_K_M (~9 GB).

**Alternativas descartadas.** Qwen2.5-14B: requiere descarga y no aporta ventaja
medida; queda como plan B. Un 7-8B: menos calidad en extracción estructurada.

**Consecuencias.** 122 tok/s de generación y JSON limpio en la prueba real. Es
un modelo de razonamiento: consume tokens en el canal de razonamiento, así que
los consumidores deben fijar `reasoning_effort` bajo o `max_tokens` amplio.

---

## 2026-11-01 · Exposición: loopback + túnel SSH

**Contexto.** El montaje original servía `llama-server` con `--host 0.0.0.0`,
sin `--api-key`, con tres reglas de firewall `Allow` en perfil `Any`, y CORS `*`
por defecto. Cualquier equipo de la LAN podía usar el modelo.

**Decisión.** `--host 127.0.0.1` + `--api-key` (fichero a 0600 equivalente, ACL
solo al usuario) + acceso desde el Mac por `ssh -L`. Las tres reglas de firewall
de 8080 se **borraron**.

**Alternativas descartadas.** LAN + `--api-key` + TLS autofirmado: superficie
permanente y certificados que mantener. LAN + firewall por IP de origen: frágil
ante DHCP y cambios de red.

**Consecuencias.** Cero superficie en LAN; el DNS `host-1.local` deja de
ser necesario para el modelo (sigue sirviendo para SSH). Si algún día se
necesita acceso sin túnel, hay que volver a abrir puerto y clave: es una
decisión nueva, no un ajuste.

---

## 2026-11-01 · Runtime: llama.cpp nativo en Windows con Vulkan

**Contexto.** El build existente estaba en WSL2 Ubuntu (CPU). La GPU es una AMD
RX 6900 XT; ROCm no soporta `gfx1030` en WSL2 de forma oficial, así que `-ngl 99`
nunca pudo funcionar. WSL además tenía `networkingMode=mirrored`, `/mnt/c`
montado y salida a internet: no era un perímetro.

**Decisión.** Descargar el binario oficial de la nightly (b12032, `win-vulkan-x64`)
y servir desde Windows nativo con Vulkan.

**Alternativas descartadas.** Seguir en WSL con CPU: 4-8 tok/s, inservible para
iterar. WSL + fork comunitario de ROCm para RDNA2: dependencia no oficial.

**Consecuencias.** 122 tok/s de generación medidos; desaparece la capa WSL del
camino crítico. El build de WSL queda como respaldo, sin usar.

---

## 2026-11-01 · Control del ciclo: tarea programada AtLogOn

**Contexto.** El servidor se lanzaba a mano desde una terminal; no había unidad
systemd ni tarea. Tras reiniciar, no había modelo.

**Decisión.** Tarea `llama-server` (AtLogOn, Interactive, RunLevel Highest, sin
límite de tiempo) que ejecuta `C:\Users\usuario\.llama\start-llama.ps1`, con
logs en `.llama\llama.*.log`. Se controla desde el Mac con
`remote.py task equipo start|stop|state llama-server`.

**Alternativas descartadas.** Servicio de Windows con NSSM: otra dependencia
para lo mismo. Arrancar desde WSL con systemd: reintroduce la capa descartada.

**Consecuencias.** Requiere sesión iniciada en Perdigon (mismo patrón que
`OpenSSH-User`). **Verificado con reinicio real el 2026-11-01: el arranque no es
desatendido.** Arranque 10:21, `AutoAdminLogon` sin definir, y la sesión de
consola no existe hasta que el usuario desbloquea con su PIN (10:29). Mientras la
pantalla está bloqueada, `sshd` **no escucha**: el host es inalcanzable por red.
Ver la entrada siguiente.

---

## 2026-11-01 · Enrutado: `modelgate` con perfiles y política

**Contexto.** Había que industrializar «este trabajo va al modelo local y este
otro a la suscripción cloud», sin depender de la memoria del agente ni de
conmutar configuración a mano.

**Decisión.** CLI `modelgate` (stdlib, patrón `net-conn`) con `models.json` no
versionado, perfiles `openai`/`hermes`, transporte `ssh-tunnel` por defecto y
regla dura `local-only` → solo perfiles `local-only`, sin caída automática.

**Alternativas descartadas.** Solo perfiles de Hermes conmutables: no deja
política ejecutable ni auditoría, y no sirve para otros consumidores (scripts,
cron). Wrapper ad-hoc en cada proyecto: duplicación.

**Consecuencias.** Un único punto donde se decide el destino, con códigos de
salida estables y auditoría de hashes. El contenido del documento nunca se
registra.
