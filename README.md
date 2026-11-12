# Calco

Un muro de enlaces de cultura, ideas y tecnología, con el centro de gravedad en España, Europa y América Latina. Réplica personal de [Clone](https://clone.fyi).

## Cómo funciona

Cada hora, GitHub Actions ejecuta `scripts/update.py`:

1. Lee los feeds RSS de `feeds.json` (sin coste en tokens).
2. Se queda con lo publicado en las últimas 48 horas que tenga fecha y hora.
3. Enseña a un modelo pequeño solo los titulares que no ha visto antes; el modelo devuelve los que se quedan, con su tema.
4. Elige dos titulares de la jornada (como mucho cada 3 horas).
5. Escribe `docs/links.js`, que la página `docs/index.html` muestra tal cual.

`state/seen.json` recuerda qué titulares ya pasaron por el modelo para no volver a pagarlos. `docs/status.json` dice qué feeds respondieron en la última ejecución.

## Puesta en marcha

1. **Clave del modelo:** Settings → Secrets and variables → Actions → New repository secret, con nombre `ANTHROPIC_API_KEY`. Sin clave, Calco funciona con reglas simples en lugar del modelo.
2. **Página:** Settings → Pages → Deploy from a branch → `main`, carpeta `/docs`.
3. **Primera ejecución:** Actions → Actualizar Calco → Run workflow.

## Ajustes

- Fuentes: `feeds.json`. `m: "all"` es una revista de la que casi todo interesa; `m: "filter"` es un medio generalista del que solo se quiere lo mejor. `max` limita los enlaces por fuente.
- Criterio editorial: la constante `CRITERIA` de `scripts/update.py`.
- Modelo: variable de entorno `CALCO_MODEL` (por defecto `claude-haiku-5-5`).
