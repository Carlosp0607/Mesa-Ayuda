import os
import io
import csv
import random
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request, Response, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader
from sqlalchemy import text

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mesa-ayuda-demo")

# Conexión a la base de datos
db_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.json.ensure_ascii = False

db = SQLAlchemy(app)

# SLA: horas máximas para resolver según la prioridad
SLA_HORAS = {"alta": 4, "media": 24, "baja": 72}

# Valores permitidos para cada campo
VALORES = {
    "tipo": ["incidente", "requerimiento"],
    "categoria": ["acceso", "error", "datos", "rendimiento", "consulta"],
    "prioridad": ["alta", "media", "baja"],
}

# Textos y colores para la interfaz
ETIQUETAS_ESTADO = {
    "abierto": "Abierto", "en_proceso": "En proceso", "escalado": "Escalado",
    "cerrado": "Cerrado", "anulado": "Anulado",
}
COLOR_ESTADO = {
    "abierto": "primary", "en_proceso": "warning", "escalado": "danger",
    "cerrado": "success", "anulado": "dark",
}
COLOR_PRIORIDAD = {"alta": "danger", "media": "warning", "baja": "secondary"}
EVENTOS = {
    "creado": {"nombre": "Ticket creado", "icono": "bi-plus-lg", "color": "primary"},
    "atendido": {"nombre": "En atención (nivel 1)", "icono": "bi-person-check", "color": "warning"},
    "escalado": {"nombre": "Escalado a nivel 2", "icono": "bi-arrow-up", "color": "danger"},
    "comentario": {"nombre": "Seguimiento", "icono": "bi-chat-left-text", "color": "secondary"},
    "editado": {"nombre": "Ticket editado", "icono": "bi-pencil", "color": "info"},
    "cerrado": {"nombre": "Solución", "icono": "bi-check-lg", "color": "success"},
    "anulado": {"nombre": "Ticket anulado", "icono": "bi-x-lg", "color": "dark"},
}

# Estados en los que el ticket ya no se puede modificar
ESTADOS_FINALES = ["cerrado", "anulado"]

# La base guarda la hora en UTC; la interfaz la muestra en hora de Colombia
HORAS_COLOMBIA = -5


def ahora():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------- MODELOS ----------------

class Ticket(db.Model):
    __tablename__ = "tickets"
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.Text, nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    categoria = db.Column(db.String(30), nullable=False)
    prioridad = db.Column(db.String(10), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="abierto")
    nivel = db.Column(db.Integer, nullable=False, default=1)
    solucion = db.Column(db.Text)
    creado_en = db.Column(db.DateTime, nullable=False, default=ahora)
    cerrado_en = db.Column(db.DateTime)
    seguimientos = db.relationship("Seguimiento", backref="ticket", order_by="Seguimiento.creado_en", lazy=True)


class Seguimiento(db.Model):
    __tablename__ = "seguimientos"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    texto = db.Column(db.Text, nullable=False)
    creado_en = db.Column(db.DateTime, nullable=False, default=ahora)


class Articulo(db.Model):
    __tablename__ = "base_conocimiento"
    id = db.Column(db.Integer, primary_key=True)
    categoria = db.Column(db.String(30), nullable=False)
    problema = db.Column(db.String(200), nullable=False)
    solucion = db.Column(db.Text, nullable=False)


with app.app_context():
    db.create_all()


# ---------------- FUNCIONES DE APOYO ----------------

def estado_sla(t):
    vence_en = t.creado_en + timedelta(hours=SLA_HORAS[t.prioridad])
    if t.estado == "cerrado":
        cumplido = t.cerrado_en <= vence_en
    else:
        cumplido = ahora() <= vence_en
    return vence_en, cumplido


def info_sla(t):
    vence_en, cumplido = estado_sla(t)
    if t.estado == "anulado":
        return {"texto": "No aplica", "color": "secondary", "vence": vence_en}
    if t.estado == "cerrado":
        if cumplido:
            return {"texto": "Cumplido", "color": "success", "vence": vence_en}
        return {"texto": "Incumplido", "color": "danger", "vence": vence_en}
    if not cumplido:
        return {"texto": "Vencido", "color": "danger", "vence": vence_en}
    horas = (vence_en - ahora()).total_seconds() / 3600
    restante = f"Quedan {horas:.0f} h" if horas >= 1 else f"Quedan {horas * 60:.0f} min"
    return {"texto": restante, "color": "warning" if horas < 2 else "success", "vence": vence_en}


def ticket_a_dict(t):
    vence_en, cumplido = estado_sla(t)
    return {
        "id": t.id,
        "titulo": t.titulo,
        "descripcion": t.descripcion,
        "tipo": t.tipo,
        "categoria": t.categoria,
        "prioridad": t.prioridad,
        "estado": t.estado,
        "nivel": t.nivel,
        "solucion": t.solucion,
        "creado_en": t.creado_en.isoformat(),
        "cerrado_en": t.cerrado_en.isoformat() if t.cerrado_en else None,
        "sla_vence_en": vence_en.isoformat(),
        "sla_cumplido": cumplido,
    }


def articulo_a_dict(a):
    return {"id": a.id, "categoria": a.categoria, "problema": a.problema, "solucion": a.solucion}


def buscar_ticket(ticket_id):
    return db.session.get(Ticket, ticket_id)


def registrar(ticket, tipo, texto):
    db.session.add(Seguimiento(ticket_id=ticket.id, tipo=tipo, texto=texto))


def linea_de_tiempo(t):
    eventos = [{"tipo": "creado", "texto": t.descripcion, "fecha": t.creado_en}]
    eventos += [{"tipo": s.tipo, "texto": s.texto, "fecha": s.creado_en} for s in t.seguimientos]
    if t.estado == "cerrado" and not any(s.tipo == "cerrado" for s in t.seguimientos):
        eventos.append({"tipo": "cerrado", "texto": t.solucion or "", "fecha": t.cerrado_en})
    return sorted(eventos, key=lambda e: e["fecha"])


# Acciones del flujo de soporte. Las usan la API y la interfaz web.
# Devuelven un mensaje de error, o None si todo salió bien.

CAMPOS_TICKET = ["titulo", "descripcion", "tipo", "categoria", "prioridad"]


def validar_datos(datos):
    if any(not datos.get(c) for c in CAMPOS_TICKET):
        return "Completa todos los campos"
    if any(datos[c] not in VALORES[c] for c in VALORES):
        return "Hay un valor no válido en el formulario"
    return None


def accion_editar(t, datos):
    if t.estado in ESTADOS_FINALES:
        return f"No se puede editar un ticket {t.estado}"
    error = validar_datos(datos)
    if error:
        return error

    cambios = []
    if datos["titulo"] != t.titulo:
        cambios.append(f"Título: {t.titulo} → {datos['titulo']}")
    if datos["descripcion"] != t.descripcion:
        cambios.append("Descripción actualizada")
    nombres = {"tipo": "Tipo", "categoria": "Categoría", "prioridad": "Prioridad"}
    for campo, nombre in nombres.items():
        if datos[campo] != getattr(t, campo):
            cambios.append(f"{nombre}: {getattr(t, campo)} → {datos[campo]}")
    if datos["prioridad"] != t.prioridad:
        cambios.append(f"Nuevo límite de SLA: {SLA_HORAS[datos['prioridad']]} h desde la creación")
    if not cambios:
        return "No hubo cambios para guardar"

    for campo in CAMPOS_TICKET:
        setattr(t, campo, datos[campo])
    registrar(t, "editado", "\n".join(cambios))
    return None


def accion_anular(t, motivo):
    if t.estado == "anulado":
        return "El ticket ya está anulado"
    if t.estado == "cerrado":
        return "No se puede anular un ticket cerrado"
    if not motivo:
        return "Para anular el ticket debes escribir el motivo"
    t.estado = "anulado"
    registrar(t, "anulado", motivo)
    return None


def accion_atender(t):
    if t.estado != "abierto":
        return f"Solo se atienden tickets abiertos. Estado actual: {t.estado}"
    t.estado = "en_proceso"
    registrar(t, "atendido", "El caso pasó a en proceso")
    return None


def accion_escalar(t, motivo=""):
    if t.estado in ESTADOS_FINALES:
        return f"No se puede escalar un ticket {t.estado}"
    if t.nivel == 2:
        return "El ticket ya está en nivel 2"
    t.nivel = 2
    t.estado = "escalado"
    registrar(t, "escalado", motivo or "Pasa a soporte de nivel 2")
    return None


def accion_cerrar(t, solucion, documentar):
    if t.estado == "cerrado":
        return "El ticket ya está cerrado"
    if t.estado == "anulado":
        return "No se puede cerrar un ticket anulado"
    if not solucion:
        return "Para cerrar el ticket debes escribir la solución"
    t.solucion = solucion
    t.estado = "cerrado"
    t.cerrado_en = ahora()
    registrar(t, "cerrado", solucion)
    if documentar:
        db.session.add(Articulo(categoria=t.categoria, problema=t.titulo, solucion=solucion))
    return None


# ---------------- PLANTILLAS HTML ----------------

PLANTILLAS = {}

PLANTILLAS["base.html"] = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block titulo %}Mesa de Ayuda Carlos Daniel{% endblock %}</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css" rel="stylesheet">
<style>
  body { background: #f1f3f7; }
  .sidebar { width: 240px; min-height: 100vh; background: #1f2d3d; flex-shrink: 0; }
  .sidebar a { color: #c8d1dc; text-decoration: none; display: block; padding: .6rem 1rem; border-radius: .4rem; margin-bottom: .2rem; }
  .sidebar a:hover, .sidebar a.activo { background: #2e4258; color: #fff; }
  .sidebar a.btn-nuevo { background: #0d6efd; color: #fff; text-align: center; margin-bottom: 1rem; }
  .sidebar a.btn-nuevo:hover { background: #0b5ed7; }
  .barra-movil { background: #1f2d3d; }
  .barra-movil a { color: #c8d1dc; text-decoration: none; margin-right: 1rem; }
  .kpi { border: 0; border-left: 4px solid var(--c); }
  .timeline { border-left: 2px solid #dee2e6; margin-left: .85rem; padding-left: 1.6rem; }
  .evento { position: relative; margin-bottom: 1.4rem; }
  .evento .punto { position: absolute; left: -2.5rem; top: -.1rem; width: 1.8rem; height: 1.8rem; border-radius: 50%;
                   display: flex; align-items: center; justify-content: center; color: #fff; font-size: .9rem; }
  main { min-width: 0; }
</style>
</head>
<body>
<div class="barra-movil d-md-none p-3">
  <div class="text-white fw-bold mb-2"><i class="bi bi-life-preserver"></i> Mesa de Ayuda <span class="fw-normal text-info">Charles'DPT</span></div>
  <a href="{{ url_for('dashboard') }}">Inicio</a>
  <a href="{{ url_for('panel_tickets') }}">Tickets</a>
  <a href="{{ url_for('panel_nuevo') }}">Nuevo</a>
  <a href="{{ url_for('panel_conocimiento') }}">Conocimiento</a>
</div>
<div class="d-flex">
  <nav class="sidebar p-3 d-none d-md-block">
    <div class="text-white fw-bold fs-5 mb-4 lh-sm"><i class="bi bi-life-preserver"></i> Mesa de Ayuda
      <div class="fs-6 fw-normal text-info mt-1 ps-4">Charles'DPT</div></div>
    <a href="{{ url_for('panel_nuevo') }}" class="btn-nuevo"><i class="bi bi-plus-lg me-1"></i>Nuevo ticket</a>
    <a href="{{ url_for('dashboard') }}" class="{{ 'activo' if seccion == 'inicio' }}"><i class="bi bi-speedometer2 me-2"></i>Inicio</a>
    <a href="{{ url_for('panel_tickets') }}" class="{{ 'activo' if seccion == 'tickets' }}"><i class="bi bi-ticket-perforated me-2"></i>Tickets</a>
    <a href="{{ url_for('panel_conocimiento') }}" class="{{ 'activo' if seccion == 'conocimiento' }}"><i class="bi bi-book me-2"></i>Base de conocimiento</a>
    <hr class="text-secondary">
    <a href="{{ url_for('exportar_csv') }}"><i class="bi bi-filetype-csv me-2"></i>Exportar CSV</a>
    <a href="{{ url_for('resumen') }}"><i class="bi bi-braces me-2"></i>API de indicadores</a>
  </nav>
  <main class="flex-grow-1 p-4">
    {% with mensajes = get_flashed_messages(with_categories=true) %}
      {% for categoria, mensaje in mensajes %}
        <div class="alert alert-{{ categoria }} alert-dismissible fade show">
          {{ mensaje }}<button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
      {% endfor %}
    {% endwith %}
    {% block contenido %}{% endblock %}
  </main>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
{% block scripts %}{% endblock %}
</body>
</html>
"""

PLANTILLAS["dashboard.html"] = """
{% extends "base.html" %}
{% block contenido %}
<h4 class="mb-4">Panel de control</h4>

<div class="row g-3 mb-4">
  {% for k in kpis %}
  <div class="col-6 col-lg">
    <div class="card kpi shadow-sm h-100" style="--c: {{ k.color }}">
      <div class="card-body">
        <div class="text-muted small">{{ k.nombre }}</div>
        <div class="fs-3 fw-bold">{{ k.valor }}</div>
      </div>
    </div>
  </div>
  {% endfor %}
</div>

<div class="row g-3 mb-4">
  <div class="col-lg-5">
    <div class="card shadow-sm h-100"><div class="card-body">
      <h6 class="mb-3">Tickets por estado</h6>
      <canvas id="graficaEstado"></canvas>
    </div></div>
  </div>
  <div class="col-lg-7">
    <div class="card shadow-sm h-100"><div class="card-body">
      <h6 class="mb-3">Tickets por categoría</h6>
      <canvas id="graficaCategoria"></canvas>
    </div></div>
  </div>
</div>

<div class="card shadow-sm"><div class="card-body">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <h6 class="mb-0">Últimos tickets</h6>
    <a href="{{ url_for('panel_tickets') }}" class="small">Ver todos</a>
  </div>
  <div class="table-responsive">
    <table class="table table-hover align-middle mb-0">
      <thead><tr><th>#</th><th>Título</th><th>Categoría</th><th>Prioridad</th><th>Estado</th><th>Creado</th></tr></thead>
      <tbody>
      {% for t in ultimos %}
        <tr>
          <td><a href="{{ url_for('panel_detalle', ticket_id=t.id) }}" class="fw-semibold text-decoration-none">#{{ t.id }}</a></td>
          <td><a href="{{ url_for('panel_detalle', ticket_id=t.id) }}" class="text-body text-decoration-none">{{ t.titulo }}</a></td>
          <td class="text-capitalize">{{ t.categoria }}</td>
          <td><span class="badge text-bg-{{ COLOR_PRIORIDAD[t.prioridad] }} text-capitalize">{{ t.prioridad }}</span></td>
          <td><span class="badge text-bg-{{ COLOR_ESTADO[t.estado] }}">{{ ETIQUETAS_ESTADO[t.estado] }}</span></td>
          <td class="small text-muted">{{ t.creado_en|fecha }}</td>
        </tr>
      {% else %}
        <tr><td colspan="6" class="text-center text-muted">Aún no hay tickets</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div></div>
{% endblock %}

{% block scripts %}
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
  const estado = {{ grafica_estado|tojson }};
  const categoria = {{ grafica_categoria|tojson }};

  new Chart(document.getElementById("graficaEstado"), {
    type: "doughnut",
    data: { labels: estado.etiquetas, datasets: [{ data: estado.valores,
      backgroundColor: ["#0d6efd", "#ffc107", "#dc3545", "#198754"] }] }
  });

  new Chart(document.getElementById("graficaCategoria"), {
    type: "bar",
    data: { labels: categoria.etiquetas, datasets: [{ label: "Tickets", data: categoria.valores,
      backgroundColor: "#2e4258" }] },
    options: { plugins: { legend: { display: false } } }
  });
</script>
{% endblock %}
"""

PLANTILLAS["tickets.html"] = """
{% extends "base.html" %}
{% block titulo %}Tickets · Mesa de Ayuda Charles'DPT{% endblock %}
{% block contenido %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0">Tickets</h4>
  <a href="{{ url_for('panel_nuevo') }}" class="btn btn-primary"><i class="bi bi-plus-lg"></i> Nuevo ticket</a>
</div>

<form class="card shadow-sm mb-3" method="get">
  <div class="card-body row g-2 align-items-end">
    <div class="col-md-4">
      <label class="form-label small text-muted">Buscar</label>
      <input name="q" class="form-control" value="{{ filtros.q or '' }}" placeholder="Título del ticket">
    </div>
    <div class="col-md-2">
      <label class="form-label small text-muted">Estado</label>
      <select name="estado" class="form-select">
        <option value="">Todos</option>
        {% for e, nombre in ETIQUETAS_ESTADO.items() %}
          <option value="{{ e }}" {{ 'selected' if filtros.estado == e }}>{{ nombre }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-md-2">
      <label class="form-label small text-muted">Prioridad</label>
      <select name="prioridad" class="form-select">
        <option value="">Todas</option>
        {% for p in VALORES.prioridad %}
          <option value="{{ p }}" {{ 'selected' if filtros.prioridad == p }}>{{ p|capitalize }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-md-2">
      <label class="form-label small text-muted">Categoría</label>
      <select name="categoria" class="form-select">
        <option value="">Todas</option>
        {% for c in VALORES.categoria %}
          <option value="{{ c }}" {{ 'selected' if filtros.categoria == c }}>{{ c|capitalize }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-md-2 d-flex gap-2">
      <button class="btn btn-dark w-100">Filtrar</button>
      <a href="{{ url_for('panel_tickets') }}" class="btn btn-outline-secondary" title="Limpiar"><i class="bi bi-x-lg"></i></a>
    </div>
  </div>
</form>

<div class="card shadow-sm"><div class="card-body">
  <div class="small text-muted mb-2">{{ paginacion.total }} tickets encontrados</div>
  <div class="table-responsive">
    <table class="table table-hover align-middle mb-0">
      <thead><tr><th>#</th><th>Título</th><th>Tipo</th><th>Categoría</th><th>Prioridad</th><th>Estado</th><th>Nivel</th><th>SLA</th><th>Creado</th></tr></thead>
      <tbody>
      {% for t in paginacion.items %}
        {% set sla = info_sla(t) %}
        <tr>
          <td><a href="{{ url_for('panel_detalle', ticket_id=t.id) }}" class="fw-semibold text-decoration-none">#{{ t.id }}</a></td>
          <td><a href="{{ url_for('panel_detalle', ticket_id=t.id) }}" class="text-body text-decoration-none">{{ t.titulo }}</a></td>
          <td class="text-capitalize small">{{ t.tipo }}</td>
          <td class="text-capitalize">{{ t.categoria }}</td>
          <td><span class="badge text-bg-{{ COLOR_PRIORIDAD[t.prioridad] }} text-capitalize">{{ t.prioridad }}</span></td>
          <td><span class="badge text-bg-{{ COLOR_ESTADO[t.estado] }}">{{ ETIQUETAS_ESTADO[t.estado] }}</span></td>
          <td>N{{ t.nivel }}</td>
          <td><span class="badge text-bg-{{ sla.color }}">{{ sla.texto }}</span></td>
          <td class="small text-muted text-nowrap">{{ t.creado_en|fecha }}</td>
        </tr>
      {% else %}
        <tr><td colspan="9" class="text-center text-muted">No hay tickets con esos filtros</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  {% if paginacion.pages > 1 %}
  <nav class="mt-3">
    <ul class="pagination pagination-sm mb-0 flex-wrap">
      {% for n in paginacion.iter_pages(left_edge=1, right_edge=1, left_current=2, right_current=2) %}
        {% if n %}
          <li class="page-item {{ 'active' if n == paginacion.page }}"><a class="page-link" href="{{ url_pagina(n) }}">{{ n }}</a></li>
        {% else %}
          <li class="page-item disabled"><span class="page-link">…</span></li>
        {% endif %}
      {% endfor %}
    </ul>
  </nav>
  {% endif %}
</div></div>
{% endblock %}
"""

PLANTILLAS["nuevo.html"] = """
{% extends "base.html" %}
{% block titulo %}{{ 'Editar ticket' if ticket_id else 'Nuevo ticket' }} · Mesa de Ayuda Charles'DPT{% endblock %}
{% block contenido %}
{% if ticket_id %}
<a href="{{ url_for('panel_detalle', ticket_id=ticket_id) }}" class="small text-decoration-none"><i class="bi bi-arrow-left"></i> Volver al ticket</a>
<h4 class="mt-2 mb-4">Editar ticket #{{ ticket_id }}</h4>
{% else %}
<h4 class="mb-4">Nuevo ticket</h4>
{% endif %}
<form method="post" class="card shadow-sm" style="max-width: 760px">
  <div class="card-body row g-3">
    <div class="col-12">
      <label class="form-label">Título</label>
      <input name="titulo" maxlength="120" class="form-control" value="{{ datos.get('titulo', '') }}" placeholder="Ej: No puedo iniciar sesión" required>
    </div>
    <div class="col-12">
      <label class="form-label">Descripción</label>
      <textarea name="descripcion" rows="4" class="form-control" placeholder="Qué pasa, desde cuándo, qué mensaje aparece" required>{{ datos.get('descripcion', '') }}</textarea>
    </div>
    <div class="col-md-4">
      <label class="form-label">Tipo</label>
      <select name="tipo" class="form-select">
        {% for v in VALORES.tipo %}
          <option value="{{ v }}" {{ 'selected' if datos.get('tipo') == v }}>{{ v|capitalize }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-md-4">
      <label class="form-label">Categoría</label>
      <select name="categoria" class="form-select">
        {% for v in VALORES.categoria %}
          <option value="{{ v }}" {{ 'selected' if datos.get('categoria') == v }}>{{ v|capitalize }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-md-4">
      <label class="form-label">Prioridad</label>
      <select name="prioridad" class="form-select">
        {% for v in VALORES.prioridad %}
          <option value="{{ v }}" {{ 'selected' if datos.get('prioridad', 'media') == v }}>{{ v|capitalize }} (SLA {{ SLA_HORAS[v] }} h)</option>
        {% endfor %}
      </select>
    </div>
    <div class="col-12 d-flex gap-2">
      {% if ticket_id %}
      <button class="btn btn-primary"><i class="bi bi-save"></i> Guardar cambios</button>
      <a href="{{ url_for('panel_detalle', ticket_id=ticket_id) }}" class="btn btn-outline-secondary">Cancelar</a>
      {% else %}
      <button class="btn btn-primary"><i class="bi bi-send"></i> Crear ticket</button>
      <a href="{{ url_for('panel_tickets') }}" class="btn btn-outline-secondary">Cancelar</a>
      {% endif %}
    </div>
  </div>
</form>
{% endblock %}
"""

PLANTILLAS["detalle.html"] = """
{% extends "base.html" %}
{% block titulo %}Ticket #{{ t.id }} · Mesa de Ayuda Charles'DPT{% endblock %}
{% block contenido %}
{% set sla = info_sla(t) %}
<a href="{{ url_for('panel_tickets') }}" class="small text-decoration-none"><i class="bi bi-arrow-left"></i> Volver a tickets</a>
<div class="d-flex flex-wrap align-items-center gap-2 mt-2 mb-4">
  <h4 class="mb-0 me-2">#{{ t.id }} {{ t.titulo }}</h4>
  <span class="badge text-bg-{{ COLOR_ESTADO[t.estado] }}">{{ ETIQUETAS_ESTADO[t.estado] }}</span>
  <span class="badge text-bg-{{ COLOR_PRIORIDAD[t.prioridad] }}">Prioridad {{ t.prioridad }}</span>
  <span class="badge text-bg-{{ sla.color }}">SLA: {{ sla.texto }}</span>
</div>

<div class="row g-3">
  <div class="col-lg-8">
    <div class="card shadow-sm"><div class="card-body">
      <h6 class="mb-4">Línea de tiempo</h6>
      <div class="timeline">
        {% for e in eventos %}
          {% set ev = EVENTOS.get(e.tipo, EVENTOS['comentario']) %}
          <div class="evento">
            <span class="punto bg-{{ ev.color }}"><i class="bi {{ ev.icono }}"></i></span>
            <div class="d-flex justify-content-between flex-wrap gap-2">
              <strong>{{ ev.nombre }}</strong>
              <span class="small text-muted">{{ e.fecha|fecha }}</span>
            </div>
            <div class="text-body-secondary" style="white-space: pre-line">{{ e.texto }}</div>
          </div>
        {% endfor %}
      </div>

      {% if t.estado not in ESTADOS_FINALES %}
      <form method="post" action="{{ url_for('panel_accion', ticket_id=t.id) }}" class="mt-2">
        <input type="hidden" name="accion" value="comentar">
        <textarea name="texto" class="form-control mb-2" rows="2" placeholder="Agregar seguimiento: qué se revisó, qué se le pidió al usuario..." required></textarea>
        <button class="btn btn-outline-dark btn-sm"><i class="bi bi-chat-left-text"></i> Agregar seguimiento</button>
      </form>
      {% endif %}
    </div></div>
  </div>

  <div class="col-lg-4">
    <div class="card shadow-sm mb-3"><div class="card-body">
      <h6 class="mb-3">Datos del ticket</h6>
      <dl class="row small mb-0">
        <dt class="col-5 text-muted fw-normal">Tipo</dt><dd class="col-7 text-capitalize">{{ t.tipo }}</dd>
        <dt class="col-5 text-muted fw-normal">Categoría</dt><dd class="col-7 text-capitalize">{{ t.categoria }}</dd>
        <dt class="col-5 text-muted fw-normal">Prioridad</dt><dd class="col-7 text-capitalize">{{ t.prioridad }} ({{ SLA_HORAS[t.prioridad] }} h)</dd>
        <dt class="col-5 text-muted fw-normal">Nivel</dt><dd class="col-7">Nivel {{ t.nivel }}</dd>
        <dt class="col-5 text-muted fw-normal">Creado</dt><dd class="col-7">{{ t.creado_en|fecha }}</dd>
        <dt class="col-5 text-muted fw-normal">Vence SLA</dt><dd class="col-7">{{ sla.vence|fecha }}</dd>
        {% if t.cerrado_en %}
        <dt class="col-5 text-muted fw-normal">Cerrado</dt><dd class="col-7">{{ t.cerrado_en|fecha }}</dd>
        {% endif %}
      </dl>
    </div></div>

    {% if t.estado not in ESTADOS_FINALES %}
    <div class="card shadow-sm mb-3"><div class="card-body">
      <h6 class="mb-3">Acciones</h6>
      <a href="{{ url_for('panel_editar', ticket_id=t.id) }}" class="btn btn-outline-primary w-100 mb-3"><i class="bi bi-pencil"></i> Editar ticket</a>
      {% if t.estado == 'abierto' %}
      <form method="post" action="{{ url_for('panel_accion', ticket_id=t.id) }}" class="mb-3">
        <input type="hidden" name="accion" value="atender">
        <button class="btn btn-warning w-100"><i class="bi bi-person-check"></i> Atender ticket</button>
      </form>
      {% endif %}
      {% if t.nivel == 1 %}
      <form method="post" action="{{ url_for('panel_accion', ticket_id=t.id) }}" class="mb-3">
        <input type="hidden" name="accion" value="escalar">
        <input name="motivo" class="form-control form-control-sm mb-2" placeholder="Motivo del escalamiento">
        <button class="btn btn-outline-danger w-100"><i class="bi bi-arrow-up-circle"></i> Escalar a nivel 2</button>
      </form>
      {% endif %}
      <form method="post" action="{{ url_for('panel_accion', ticket_id=t.id) }}">
        <input type="hidden" name="accion" value="cerrar">
        <textarea name="solucion" class="form-control form-control-sm mb-2" rows="3" placeholder="Solución aplicada (obligatoria)" required></textarea>
        <div class="form-check mb-2">
          <input class="form-check-input" type="checkbox" name="documentar" value="si" id="documentar">
          <label class="form-check-label small" for="documentar">Guardar en la base de conocimiento</label>
        </div>
        <button class="btn btn-success w-100"><i class="bi bi-check-circle"></i> Cerrar ticket</button>
      </form>
      <hr>
      <form method="post" action="{{ url_for('panel_accion', ticket_id=t.id) }}"
            onsubmit="return confirm('¿Anular este ticket? Queda en el historial pero sale de los indicadores.');">
        <input type="hidden" name="accion" value="anular">
        <input name="motivo" class="form-control form-control-sm mb-2" placeholder="Motivo de anulación (obligatorio)" required>
        <button class="btn btn-outline-dark w-100"><i class="bi bi-x-circle"></i> Anular ticket</button>
      </form>
    </div></div>
    {% endif %}

    <div class="card shadow-sm"><div class="card-body">
      <h6 class="mb-3"><i class="bi bi-lightbulb"></i> Soluciones sugeridas</h6>
      {% for a in sugeridas %}
        <div class="border-start border-3 border-info ps-2 mb-3">
          <div class="fw-semibold small">{{ a.problema }}</div>
          <div class="small text-muted">{{ a.solucion }}</div>
        </div>
      {% else %}
        <div class="small text-muted">No hay artículos para la categoría {{ t.categoria }}.</div>
      {% endfor %}
    </div></div>
  </div>
</div>
{% endblock %}
"""

PLANTILLAS["conocimiento.html"] = """
{% extends "base.html" %}
{% block titulo %}Base de conocimiento · Mesa de Ayuda Charles'DPT{% endblock %}
{% block contenido %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h4 class="mb-0">Base de conocimiento</h4>
  <button class="btn btn-primary" data-bs-toggle="collapse" data-bs-target="#formArticulo"><i class="bi bi-plus-lg"></i> Nuevo artículo</button>
</div>

<div class="collapse mb-3" id="formArticulo">
  <form method="post" class="card card-body shadow-sm">
    <div class="row g-2">
      <div class="col-md-3">
        <select name="categoria" class="form-select">
          {% for c in VALORES.categoria %}<option value="{{ c }}">{{ c|capitalize }}</option>{% endfor %}
        </select>
      </div>
      <div class="col-md-9"><input name="problema" maxlength="200" class="form-control" placeholder="Problema" required></div>
      <div class="col-12"><textarea name="solucion" rows="3" class="form-control" placeholder="Solución paso a paso" required></textarea></div>
      <div class="col-12"><button class="btn btn-success"><i class="bi bi-save"></i> Guardar artículo</button></div>
    </div>
  </form>
</div>

<div class="d-flex flex-wrap gap-2 align-items-center mb-3">
  <a href="{{ url_for('panel_conocimiento') }}" class="btn btn-sm {{ 'btn-dark' if not categoria else 'btn-outline-dark' }}">Todas</a>
  {% for c in VALORES.categoria %}
    <a href="{{ url_for('panel_conocimiento', categoria=c) }}" class="btn btn-sm {{ 'btn-dark' if categoria == c else 'btn-outline-dark' }}">{{ c|capitalize }}</a>
  {% endfor %}
  <form method="get" class="ms-md-auto d-flex gap-2">
    {% if categoria %}<input type="hidden" name="categoria" value="{{ categoria }}">{% endif %}
    <input name="q" value="{{ q or '' }}" class="form-control form-control-sm" placeholder="Buscar solución">
    <button class="btn btn-sm btn-dark"><i class="bi bi-search"></i></button>
  </form>
</div>

{% for a in articulos %}
  <div class="card shadow-sm mb-2"><div class="card-body">
    <span class="badge text-bg-info text-capitalize mb-2">{{ a.categoria }}</span>
    <div class="fw-semibold">{{ a.problema }}</div>
    <div class="text-body-secondary small" style="white-space: pre-line">{{ a.solucion }}</div>
  </div></div>
{% else %}
  <div class="text-muted">No hay artículos con esos filtros.</div>
{% endfor %}
{% endblock %}
"""

app.jinja_env.loader = DictLoader(PLANTILLAS)
app.jinja_env.globals.update(
    ETIQUETAS_ESTADO=ETIQUETAS_ESTADO, COLOR_ESTADO=COLOR_ESTADO, COLOR_PRIORIDAD=COLOR_PRIORIDAD,
    EVENTOS=EVENTOS, VALORES=VALORES, SLA_HORAS=SLA_HORAS, info_sla=info_sla,
    ESTADOS_FINALES=ESTADOS_FINALES,
)


@app.template_filter("fecha")
def formato_fecha(d):
    if not d:
        return ""
    return (d + timedelta(hours=HORAS_COLOMBIA)).strftime("%d/%m/%Y %I:%M %p")


# ---------------- INTERFAZ WEB ----------------

@app.route("/")
def dashboard():
    # Los tickets anulados no cuentan en los indicadores
    tickets = Ticket.query.filter(Ticket.estado != "anulado").all()
    total = len(tickets)
    cerrados = [t for t in tickets if t.estado == "cerrado"]

    abiertos = total - len(cerrados)
    vencidos = sum(1 for t in tickets if t.estado != "cerrado" and not estado_sla(t)[1])
    sla_ok = sum(1 for t in cerrados if estado_sla(t)[1])
    pct_sla = round(100 * sla_ok / len(cerrados), 1) if cerrados else 0
    escalados = sum(1 for t in tickets if t.nivel == 2)
    pct_escalados = round(100 * escalados / total, 1) if total else 0

    kpis = [
        {"nombre": "Total de tickets", "valor": total, "color": "#0d6efd"},
        {"nombre": "Sin cerrar", "valor": abiertos, "color": "#ffc107"},
        {"nombre": "Vencidos sin cerrar", "valor": vencidos, "color": "#dc3545"},
        {"nombre": "SLA cumplido", "valor": f"{pct_sla}%", "color": "#198754"},
        {"nombre": "Escalados a N2", "valor": f"{pct_escalados}%", "color": "#6f42c1"},
    ]

    conteo_estado = {e: 0 for e in ETIQUETAS_ESTADO if e != "anulado"}
    conteo_categoria = {c: 0 for c in VALORES["categoria"]}
    for t in tickets:
        conteo_estado[t.estado] = conteo_estado.get(t.estado, 0) + 1
        conteo_categoria[t.categoria] = conteo_categoria.get(t.categoria, 0) + 1

    grafica_estado = {
        "etiquetas": [ETIQUETAS_ESTADO.get(e, e) for e in conteo_estado],
        "valores": list(conteo_estado.values()),
    }
    grafica_categoria = {
        "etiquetas": [c.capitalize() for c in conteo_categoria],
        "valores": list(conteo_categoria.values()),
    }

    ultimos = sorted(tickets, key=lambda t: t.creado_en, reverse=True)[:8]

    return render_template(
        "dashboard.html", seccion="inicio", kpis=kpis, ultimos=ultimos,
        grafica_estado=grafica_estado, grafica_categoria=grafica_categoria,
    )


@app.route("/panel/tickets")
def panel_tickets():
    filtros = {k: request.args.get(k, "").strip() for k in ["q", "estado", "prioridad", "categoria"]}
    filtros = {k: v for k, v in filtros.items() if v}

    consulta = Ticket.query
    if "q" in filtros:
        consulta = consulta.filter(Ticket.titulo.ilike(f"%{filtros['q']}%"))
    for campo in ["estado", "prioridad", "categoria"]:
        if campo in filtros:
            consulta = consulta.filter(getattr(Ticket, campo) == filtros[campo])

    pagina = request.args.get("pagina", 1, type=int)
    paginacion = consulta.order_by(Ticket.creado_en.desc()).paginate(page=pagina, per_page=15, error_out=False)

    def url_pagina(n):
        return url_for("panel_tickets", pagina=n, **filtros)

    return render_template(
        "tickets.html", seccion="tickets", filtros=filtros, paginacion=paginacion, url_pagina=url_pagina
    )


@app.route("/panel/tickets/nuevo", methods=["GET", "POST"])
def panel_nuevo():
    if request.method == "GET":
        return render_template("nuevo.html", seccion="tickets", datos={})

    datos = {c: request.form.get(c, "").strip() for c in CAMPOS_TICKET}
    error = validar_datos(datos)
    if error:
        flash(error, "danger")
        return render_template("nuevo.html", seccion="tickets", datos=datos)

    ticket = Ticket(**datos)
    db.session.add(ticket)
    db.session.commit()
    flash(f"Ticket #{ticket.id} creado. Tiempo de SLA: {SLA_HORAS[ticket.prioridad]} horas.", "success")
    return redirect(url_for("panel_detalle", ticket_id=ticket.id))


@app.route("/panel/tickets/<int:ticket_id>")
def panel_detalle(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        flash("Ticket no encontrado", "danger")
        return redirect(url_for("panel_tickets"))

    sugeridas = Articulo.query.filter_by(categoria=ticket.categoria).limit(5).all()
    return render_template(
        "detalle.html", seccion="tickets", t=ticket, eventos=linea_de_tiempo(ticket), sugeridas=sugeridas
    )


@app.route("/panel/tickets/<int:ticket_id>/editar", methods=["GET", "POST"])
def panel_editar(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        flash("Ticket no encontrado", "danger")
        return redirect(url_for("panel_tickets"))
    if ticket.estado in ESTADOS_FINALES:
        flash(f"No se puede editar un ticket {ticket.estado}", "danger")
        return redirect(url_for("panel_detalle", ticket_id=ticket.id))

    if request.method == "GET":
        datos = {c: getattr(ticket, c) for c in CAMPOS_TICKET}
        return render_template("nuevo.html", seccion="tickets", datos=datos, ticket_id=ticket.id)

    datos = {c: request.form.get(c, "").strip() for c in CAMPOS_TICKET}
    error = accion_editar(ticket, datos)
    if error:
        db.session.rollback()
        flash(error, "danger")
        return render_template("nuevo.html", seccion="tickets", datos=datos, ticket_id=ticket.id)

    db.session.commit()
    flash("Cambios guardados", "success")
    return redirect(url_for("panel_detalle", ticket_id=ticket.id))


MENSAJES_ACCION = {
    "atender": "El ticket pasó a en proceso",
    "escalar": "El ticket se escaló a nivel 2",
    "cerrar": "Ticket cerrado",
    "comentar": "Seguimiento agregado",
    "anular": "Ticket anulado",
}


@app.route("/panel/tickets/<int:ticket_id>/accion", methods=["POST"])
def panel_accion(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        flash("Ticket no encontrado", "danger")
        return redirect(url_for("panel_tickets"))

    accion = request.form.get("accion")
    if accion == "atender":
        error = accion_atender(ticket)
    elif accion == "escalar":
        error = accion_escalar(ticket, request.form.get("motivo", "").strip())
    elif accion == "cerrar":
        error = accion_cerrar(
            ticket, request.form.get("solucion", "").strip(), request.form.get("documentar") == "si"
        )
    elif accion == "anular":
        error = accion_anular(ticket, request.form.get("motivo", "").strip())
    elif accion == "comentar":
        texto = request.form.get("texto", "").strip()
        if ticket.estado in ESTADOS_FINALES:
            error = f"No se pueden agregar seguimientos a un ticket {ticket.estado}"
        elif texto:
            registrar(ticket, "comentario", texto)
            error = None
        else:
            error = "Escribe el seguimiento antes de guardarlo"
    else:
        error = "Acción no válida"

    if error:
        db.session.rollback()
        flash(error, "danger")
    else:
        db.session.commit()
        flash(MENSAJES_ACCION[accion], "success")
    return redirect(url_for("panel_detalle", ticket_id=ticket.id))


@app.route("/panel/conocimiento", methods=["GET", "POST"])
def panel_conocimiento():
    if request.method == "POST":
        categoria = request.form.get("categoria", "")
        problema = request.form.get("problema", "").strip()
        solucion = request.form.get("solucion", "").strip()
        if categoria not in VALORES["categoria"] or not problema or not solucion:
            flash("Completa la categoría, el problema y la solución", "danger")
        else:
            db.session.add(Articulo(categoria=categoria, problema=problema, solucion=solucion))
            db.session.commit()
            flash("Artículo guardado", "success")
        return redirect(url_for("panel_conocimiento"))

    categoria = request.args.get("categoria", "")
    q = request.args.get("q", "").strip()
    consulta = Articulo.query
    if categoria:
        consulta = consulta.filter_by(categoria=categoria)
    if q:
        consulta = consulta.filter(Articulo.problema.ilike(f"%{q}%") | Articulo.solucion.ilike(f"%{q}%"))
    articulos = consulta.order_by(Articulo.id.desc()).all()

    return render_template(
        "conocimiento.html", seccion="conocimiento", articulos=articulos, categoria=categoria, q=q
    )


# ---------------- API: ESTADO ----------------

@app.route("/api/estado")
def inicio():
    return jsonify({"sistema": "Mesa de Ayuda Charles'DPT", "estado": "funcionando"})


# ---------------- API: TICKETS ----------------

@app.route("/tickets", methods=["POST"])
def crear_ticket():
    datos = request.get_json(silent=True) or {}

    obligatorios = ["titulo", "descripcion", "tipo", "categoria", "prioridad"]
    faltan = [campo for campo in obligatorios if not datos.get(campo)]
    if faltan:
        return jsonify({"error": "Faltan campos", "campos": faltan}), 400

    for campo, permitidos in VALORES.items():
        if datos[campo] not in permitidos:
            return jsonify({"error": f"Valor no válido en '{campo}'", "permitidos": permitidos}), 400

    ticket = Ticket(
        titulo=datos["titulo"],
        descripcion=datos["descripcion"],
        tipo=datos["tipo"],
        categoria=datos["categoria"],
        prioridad=datos["prioridad"],
    )
    db.session.add(ticket)
    db.session.commit()

    sugerencias = Articulo.query.filter_by(categoria=ticket.categoria).all()
    respuesta = ticket_a_dict(ticket)
    respuesta["sugerencias"] = [articulo_a_dict(a) for a in sugerencias]
    return jsonify(respuesta), 201


@app.route("/tickets", methods=["GET"])
def listar_tickets():
    consulta = Ticket.query

    estado = request.args.get("estado")
    prioridad = request.args.get("prioridad")
    categoria = request.args.get("categoria")

    if estado:
        consulta = consulta.filter_by(estado=estado)
    if prioridad:
        consulta = consulta.filter_by(prioridad=prioridad)
    if categoria:
        consulta = consulta.filter_by(categoria=categoria)

    tickets = consulta.order_by(Ticket.creado_en.desc()).all()
    return jsonify([ticket_a_dict(t) for t in tickets])


@app.route("/tickets/<int:ticket_id>", methods=["GET"])
def ver_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    return jsonify(ticket_a_dict(ticket))


# ---------------- API: FLUJO DE SOPORTE ----------------

@app.route("/tickets/<int:ticket_id>/atender", methods=["PATCH"])
def atender_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    error = accion_atender(ticket)
    if error:
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/<int:ticket_id>/escalar", methods=["PATCH"])
def escalar_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    datos = request.get_json(silent=True) or {}
    error = accion_escalar(ticket, datos.get("motivo", ""))
    if error:
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/<int:ticket_id>/cerrar", methods=["PATCH"])
def cerrar_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    datos = request.get_json(silent=True) or {}
    error = accion_cerrar(ticket, datos.get("solucion"), datos.get("documentar"))
    if error:
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/<int:ticket_id>", methods=["PATCH"])
def editar_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    datos = request.get_json(silent=True) or {}
    # Los campos que no se envían conservan su valor actual
    completos = {c: datos.get(c, getattr(ticket, c)) for c in CAMPOS_TICKET}
    error = accion_editar(ticket, completos)
    if error:
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/<int:ticket_id>/anular", methods=["PATCH"])
def anular_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    datos = request.get_json(silent=True) or {}
    error = accion_anular(ticket, datos.get("motivo"))
    if error:
        return jsonify({"error": error}), 400
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/vencidos", methods=["GET"])
def tickets_vencidos():
    tickets = Ticket.query.filter(Ticket.estado != "anulado").all()
    vencidos = [ticket_a_dict(t) for t in tickets if not estado_sla(t)[1]]
    return jsonify(vencidos)


# ---------------- API: BASE DE CONOCIMIENTO ----------------

@app.route("/conocimiento", methods=["POST"])
def crear_articulo():
    datos = request.get_json(silent=True) or {}
    faltan = [c for c in ["categoria", "problema", "solucion"] if not datos.get(c)]
    if faltan:
        return jsonify({"error": "Faltan campos", "campos": faltan}), 400
    if datos["categoria"] not in VALORES["categoria"]:
        return jsonify({"error": "Categoría no válida", "permitidos": VALORES["categoria"]}), 400

    articulo = Articulo(categoria=datos["categoria"], problema=datos["problema"], solucion=datos["solucion"])
    db.session.add(articulo)
    db.session.commit()
    return jsonify(articulo_a_dict(articulo)), 201


@app.route("/conocimiento", methods=["GET"])
def listar_articulos():
    consulta = Articulo.query
    categoria = request.args.get("categoria")
    if categoria:
        consulta = consulta.filter_by(categoria=categoria)
    return jsonify([articulo_a_dict(a) for a in consulta.all()])


# ---------------- DATOS: TICKETS SIMULADOS ----------------

TITULOS = {
    "acceso": ["No puedo iniciar sesión", "Usuario bloqueado", "Olvidé mi contraseña", "Solicitud de usuario nuevo"],
    "error": ["Error 500 al guardar", "El botón de enviar no responde", "Error al cargar archivo"],
    "datos": ["El reporte no cuadra", "Registro duplicado", "No aparece un registro ingresado"],
    "rendimiento": ["El sistema está lento", "La página tarda en cargar", "Se cae al generar reportes"],
    "consulta": ["Cómo exportar a Excel", "Cómo cambiar mi correo", "Dónde veo mis casos"],
}


def generar_ticket_simulado():
    categoria = random.choice(list(TITULOS))
    titulo = random.choice(TITULOS[categoria])
    tipo = "requerimiento" if categoria == "consulta" or titulo.startswith("Solicitud") else "incidente"
    prioridad = random.choices(["alta", "media", "baja"], weights=[20, 50, 30])[0]
    creado = ahora() - timedelta(
        days=random.randint(0, 89), hours=random.randint(0, 23), minutes=random.randint(0, 59)
    )
    escalado = random.random() < 0.2

    ticket = Ticket(
        titulo=titulo,
        descripcion=f"Caso simulado: {titulo.lower()}",
        tipo=tipo,
        categoria=categoria,
        prioridad=prioridad,
        creado_en=creado,
        nivel=2 if escalado else 1,
    )

    horas = random.uniform(0.3, SLA_HORAS[prioridad] * 1.4)
    if escalado:
        horas *= 1.5
    cierre = creado + timedelta(hours=horas)

    if cierre <= ahora() and random.random() < 0.9:
        ticket.estado = "cerrado"
        ticket.cerrado_en = cierre
        ticket.solucion = "Caso resuelto (dato simulado)"
    else:
        ticket.estado = "escalado" if escalado else random.choice(["abierto", "en_proceso"])
    return ticket


@app.route("/admin/generar-datos", methods=["POST"])
def generar_datos():
    clave = os.environ.get("ADMIN_TOKEN")
    if not clave or request.headers.get("X-Admin-Token") != clave:
        return jsonify({"error": "No autorizado"}), 403

    cantidad = min(request.args.get("cantidad", 300, type=int), 1000)
    tickets = [generar_ticket_simulado() for _ in range(cantidad)]
    db.session.add_all(tickets)
    db.session.commit()
    return jsonify({"mensaje": f"Se crearon {cantidad} tickets simulados"}), 201


# ---------------- DATOS: REPORTES ----------------

@app.route("/reportes/tickets.csv", methods=["GET"])
def exportar_csv():
    salida = io.StringIO()
    escritor = csv.writer(salida)
    escritor.writerow([
        "id", "titulo", "tipo", "categoria", "prioridad", "estado", "nivel",
        "creado_en", "cerrado_en", "horas_resolucion", "sla_horas", "sla_cumplido",
    ])
    for t in Ticket.query.order_by(Ticket.id).all():
        horas = round((t.cerrado_en - t.creado_en).total_seconds() / 3600, 1) if t.cerrado_en else ""
        if t.estado == "anulado":
            cumplido = "no aplica"
        else:
            cumplido = "si" if estado_sla(t)[1] else "no"
        escritor.writerow([
            t.id, t.titulo, t.tipo, t.categoria, t.prioridad, t.estado, t.nivel,
            t.creado_en.strftime("%Y-%m-%d %H:%M"),
            t.cerrado_en.strftime("%Y-%m-%d %H:%M") if t.cerrado_en else "",
            horas, SLA_HORAS[t.prioridad], cumplido,
        ])
    return Response(
        "\ufeff" + salida.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=tickets.csv"},
    )


def consultar(sql):
    filas = db.session.execute(text(sql)).mappings().all()
    return [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in fila.items()} for fila in filas]


@app.route("/reportes/resumen", methods=["GET"])
def resumen():
    por_estado = consultar("""
        SELECT estado, COUNT(*) AS total
        FROM tickets
        WHERE estado <> 'anulado'
        GROUP BY estado
        ORDER BY total DESC
    """)

    por_categoria = consultar("""
        SELECT categoria, COUNT(*) AS total
        FROM tickets
        WHERE estado <> 'anulado'
        GROUP BY categoria
        ORDER BY total DESC
    """)

    sla_por_prioridad = consultar("""
        SELECT
            prioridad,
            COUNT(*) AS cerrados,
            ROUND(AVG(EXTRACT(EPOCH FROM (cerrado_en - creado_en)) / 3600)::numeric, 1) AS horas_promedio,
            ROUND(100.0 * SUM(
                CASE WHEN EXTRACT(EPOCH FROM (cerrado_en - creado_en)) / 3600 <=
                    CASE prioridad WHEN 'alta' THEN 4 WHEN 'media' THEN 24 ELSE 72 END
                THEN 1 ELSE 0 END
            ) / COUNT(*), 1) AS porcentaje_sla_cumplido
        FROM tickets
        WHERE estado = 'cerrado'
        GROUP BY prioridad
        ORDER BY prioridad
    """)

    problemas_repetidos = consultar("""
        SELECT titulo, COUNT(*) AS veces
        FROM tickets
        WHERE estado <> 'anulado'
        GROUP BY titulo
        ORDER BY veces DESC
        LIMIT 5
    """)

    escalamiento = consultar("""
        SELECT ROUND(100.0 * SUM(CASE WHEN nivel = 2 THEN 1 ELSE 0 END) / COUNT(*), 1) AS porcentaje_escalados
        FROM tickets
        WHERE estado <> 'anulado'
    """)

    return jsonify({
        "tickets_por_estado": por_estado,
        "tickets_por_categoria": por_categoria,
        "sla_por_prioridad": sla_por_prioridad,
        "top_5_problemas": problemas_repetidos,
        "escalamiento": escalamiento[0] if escalamiento else {},
    })
