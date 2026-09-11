# Dashboard de monitoreo

Panel web que lee la base compartida del hub (`~/.claude-projects-hub/hub.db`) en
**solo lectura** y muestra el estado de orquestación de Nexus:

- 📊 **Métricas** globales — proyectos, capacidades, requerimientos abiertos, pendientes.
- ✅ **Pendientes (to-do)** — todo handoff `pending` + todo `set_state` con `[PEND]`, con
  el proyecto al que le toca mover.
- 🧵 **Requerimientos y su recorrido** — agrupa handoffs, estados, consultas y sesiones por
  el `RC-xxxx` que aparezca en el texto, con el flujo entre proyectos
  (`checkempresa → respaldos-scraps → checkempresa → agrotop`) y la cronología completa.
- 🔎 **Filtro** transversal — buscador de texto + proyecto + "solo abiertos", aplica a la
  pestaña activa y se recuerda en `localStorage`.

Organizado en **4 pestañas** (`#todo`, `#req`, `#mapa`, `#act`; la pestaña queda en el hash,
así que el link es compartible) con la identidad visual RedCapital: naranjo `#F5821F`, navy
`#1D2233`, cabeceras de tabla navy y barra naranja por sección, igual que la skill
`doc-redcapital`. Tema claro/oscuro automático.
- 🕸️ **Grafo** de dependencias e interacciones entre proyectos (SVG, layout circular).
- 🧭 **Ruteo resuelto** — qué consume cada proyecto y quién lo provee.
- 🔌 **Capacidades por proyecto** — provee / consume.
- 🌿 **Features coordinadas** — estado de la rama por repo (`planned → … → merged`).
- 🔁 **Interacciones recientes**.

Sin dependencias externas: solo la biblioteca estándar de Python (3.10+).

## Uso

```powershell
python dashboard\dashboard.py            # sirve en http://localhost:8788
python dashboard\dashboard.py --port 9000
python dashboard\dashboard.py --once     # imprime el HTML una vez y sale
```

El panel se regenera leyendo la BD en cada request, así siempre está fresco. El
front hace polling a `/version` (el `mtime` de la BD) y recarga solo cuando algo
cambia, sin recargar a ciegas.

## Endpoints

| Ruta | Devuelve |
|---|---|
| `/` | El panel HTML completo |
| `/version` | Marca de versión (mtime de `hub.db`) para el auto-reload |

## Notas

- Abre la BD con `mode=ro` (solo lectura): nunca escribe en el hub.
- Escucha solo en `127.0.0.1` (no expuesto a la red).
- Tema claro/oscuro automático según el sistema (`prefers-color-scheme`).
- La trazabilidad vive en `tickets.py` (agrupación + render). Autochequeo:
  `python dashboard\tickets.py`.
- El ticket se detecta con `RC[-_ ]?\d{3,5}` sobre el `stage`/payload del handoff, la clave
  y el valor de `set_state`, el `intent` de la interacción y la rama de la sesión. Para que
  un requerimiento aparezca completo, basta nombrar el RC en esos campos.
