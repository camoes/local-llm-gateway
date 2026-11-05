# AGENTS.md — modelgate

CLI **reutilizable** de enrutado de trabajo entre modelo local (`llama.cpp` en
Perdigon) y cloud (`hermes` con la suscripción). Los proyectos la referencian
con `$(MODELGATE)`; **no se copia** dentro de ellos.

## Comandos

| Comando | Qué hace |
|---|---|
| `make verify` | **Gate de "hecho"**: compila, ejecuta los 20 tests y comprueba los destinos vivos |
| `make test` | Solo unitarios (sin red) |
| `SKIP_LIVE=1 make verify` | Gate sin la prueba en vivo (Perdigon apagado) |
| `python3 modelgate.py list` | Perfiles configurados |
| `python3 modelgate.py status` | ¿Responde cada destino? Exit 4 si alguno no |
| `python3 modelgate.py tunnel up\|down\|status [perfil]` | Túnel SSH bajo demanda |
| `python3 modelgate.py run <perfil> --prompt/--file` | Ejecuta en ese perfil |
| `python3 modelgate.py route --sensitivity local-only` | Elige el primer perfil que acepte ese nivel |
| `python3 modelgate.py docs <path> --kind <tipo>` | Extrae datos de documentos con el perfil local (delega en `docpipe`) |
| `python3 modelgate.py chat` | Sesión conversacional con streaming (historial en RAM) |
| `python3 modelgate.py chat --prompt "..."` | Un turno y sale, para scripts |
| `python3 modelgate.py chat --context <path>` | Carga el texto de esa carpeta como contexto antes de conversar |
| `python3 modelgate.py chat --root <dir>` | Habilita `/docs` y `/leer` para cargar un documento concreto |
| `python3 modelgate.py chat --tools web` | Permite al modelo buscar en internet (la consulta sale del Mac) |
| `python3 modelgate.py chat --source equipo --tools web` | Todo en el host: infiere, decide y busca allí; el Mac solo pinta |
| `python3 modelgate.py model up\|down\|status\|off` | Ciclo de vida: WoL, arranque, parada y apagado del host |

## Contrato

- Códigos de salida: `0` correcto, `1` genérico, `2` configuración, `3` política,
  `4` destino inaccesible. Cambiarlos rompe a los consumidores.
- `models.json` es configuración real y **no se versiona**; la plantilla es
  `models.json.example`.
- **Regla dura**: `local-only` nunca va a un perfil `cloud-ok`. No añadir
  excepciones ni «forzar»: si hace falta, se crea otro perfil local.
- Los secretos no se copian a local: `api_key.source: ssh-file` los lee del host
  remoto en cada ejecución.
- `net-conn` se importa por ruta (`NETCONN`, por defecto `~/Repos/net-conn`);
  no duplicar lógica de SSH/WoL.

## Verificación

- `make verify` en verde antes de decir "hecho".
- La prueba en vivo exige Perdigon encendido y la tarea `llama-server` activa:
  `python3 ~/Repos/net-conn/remote.py ensure equipo` y
  `python3 ~/Repos/net-conn/remote.py task equipo state llama-server`.
- No ejecutar contra hosts ni reiniciar servicios sin aprobación explícita.

## Reglas

- Nunca commitear `models.json` ni claves.
- Sin dependencias externas: stdlib de Python 3.
- La auditoría (`~/.modelgate/audit.jsonl`) no guarda contenido, solo hashes.
  Mantenerlo así. `chat` registra el evento, nunca los mensajes.
- Todo proceso hijo que herede stdin debe recibirlo cerrado (`stdin=DEVNULL`) si
  no es interactivo: un `ssh` que hereda stdin se come la entrada del proceso
  padre y rompe cualquier REPL. Ya pasó con `ssh -N` y con `ssh_out` de net-conn.

## Decisiones

`DECISIONS.md` guarda decisiones, alternativas descartadas y consecuencias. Antes
de revertir una, añade una entrada nueva fechada; no edites la antigua.
