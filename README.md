# Mesa de Ayuda

Sistema de tickets de soporte técnico con API en Flask, base de datos PostgreSQL y análisis de indicadores (SLA, escalamiento, incidentes recurrentes).

**Demo:** https://mesa-ayuda-4wfr.onrender.com
(Plan gratuito: la primera visita puede tardar unos 50 segundos mientras el servidor despierta.)

## Qué hace

- **Soporte:** registra incidentes y requerimientos con prioridad, controla el ciclo de vida del ticket (abierto → en proceso → escalado → cerrado), escala del nivel 1 al nivel 2 y exige documentar la solución para cerrar.
- **SLA:** calcula la fecha límite de cada ticket según su prioridad (alta 4 h, media 24 h, baja 72 h) y detecta los vencidos.
- **Base de conocimiento:** guarda soluciones documentadas y las sugiere al crear un ticket de la misma categoría.
- **Datos:** exporta los tickets a CSV y calcula indicadores con SQL: tickets por estado y categoría, horas promedio de solución, porcentaje de SLA cumplido, tasa de escalamiento y problemas más repetidos.

## Tecnologías

Python · Flask · Flask-SQLAlchemy · PostgreSQL (Aiven) · Render · Excel / Power BI

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Estado del sistema |
| POST | `/tickets` | Crear ticket (devuelve sugerencias de la base de conocimiento) |
| GET | `/tickets` | Listar tickets. Filtros: `?estado=`, `?prioridad=`, `?categoria=` |
| GET | `/tickets/<id>` | Ver un ticket |
| PATCH | `/tickets/<id>/atender` | Pasar de abierto a en proceso |
| PATCH | `/tickets/<id>/escalar` | Escalar a nivel 2 |
| PATCH | `/tickets/<id>/cerrar` | Cerrar con solución (`"documentar": true` la guarda en la base de conocimiento) |
| GET | `/tickets/vencidos` | Tickets con SLA incumplido |
| POST | `/conocimiento` | Crear artículo de la base de conocimiento |
| GET | `/conocimiento` | Listar artículos. Filtro: `?categoria=` |
| GET | `/reportes/resumen` | Indicadores calculados con SQL |
| GET | `/reportes/tickets.csv` | Exportar tickets a CSV |

## Ejemplo: crear un ticket

```json
POST /tickets
{
  "titulo": "No puedo iniciar sesión",
  "descripcion": "El sistema dice contraseña incorrecta",
  "tipo": "incidente",
  "categoria": "acceso",
  "prioridad": "alta"
}
```

## Análisis de datos

El archivo `Dashboard_MesaAyuda.xlsx` contiene el tablero de indicadores en Excel, construido a partir del CSV que exporta la API.

## Variables de entorno

| Variable | Uso |
|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL |
| `ADMIN_TOKEN` | Clave para la ruta de carga de datos de prueba |

## Autor

Carlos Daniel Polanco Turizo · [LinkedIn](https://www.linkedin.com/in/carlosdanielpolanco) · [GitHub](https://github.com/Carlosp0607)
