# Dashboard de monitoreo

Panel web que lee la base compartida del hub (`~/.claude-projects-hub/hub.db`) en
**solo lectura** y muestra el estado de orquestación de Nexus:

- 📊 **Métricas** globales — proyectos, capacidades, interacciones, features coordinadas.
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
