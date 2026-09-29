import os
import io
import csv
import random
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

app = Flask(__name__)

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


# ---------------- RUTAS: TICKETS ----------------

@app.route("/")
def inicio():
    return jsonify({"sistema": "Mesa de Ayuda", "estado": "funcionando"})


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


# ---------------- RUTAS: FLUJO DE SOPORTE ----------------

@app.route("/tickets/<int:ticket_id>/atender", methods=["PATCH"])
def atender_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    if ticket.estado != "abierto":
        return jsonify({"error": f"Solo se atienden tickets abiertos. Estado actual: {ticket.estado}"}), 400

    ticket.estado = "en_proceso"
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/<int:ticket_id>/escalar", methods=["PATCH"])
def escalar_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    if ticket.estado == "cerrado":
        return jsonify({"error": "No se puede escalar un ticket cerrado"}), 400
    if ticket.nivel == 2:
        return jsonify({"error": "El ticket ya está en nivel 2"}), 400

    ticket.nivel = 2
    ticket.estado = "escalado"
    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/<int:ticket_id>/cerrar", methods=["PATCH"])
def cerrar_ticket(ticket_id):
    ticket = buscar_ticket(ticket_id)
    if ticket is None:
        return jsonify({"error": "Ticket no encontrado"}), 404
    if ticket.estado == "cerrado":
        return jsonify({"error": "El ticket ya está cerrado"}), 400

    datos = request.get_json(silent=True) or {}
    solucion = datos.get("solucion")
    if not solucion:
        return jsonify({"error": "Para cerrar el ticket debes escribir la solución"}), 400

    ticket.solucion = solucion
    ticket.estado = "cerrado"
    ticket.cerrado_en = ahora()

    if datos.get("documentar"):
        articulo = Articulo(categoria=ticket.categoria, problema=ticket.titulo, solucion=solucion)
        db.session.add(articulo)

    db.session.commit()
    return jsonify(ticket_a_dict(ticket))


@app.route("/tickets/vencidos", methods=["GET"])
def tickets_vencidos():
    tickets = Ticket.query.all()
    vencidos = [ticket_a_dict(t) for t in tickets if not estado_sla(t)[1]]
    return jsonify(vencidos)


# ---------------- RUTAS: BASE DE CONOCIMIENTO ----------------

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
        cumplido = estado_sla(t)[1]
        escritor.writerow([
            t.id, t.titulo, t.tipo, t.categoria, t.prioridad, t.estado, t.nivel,
            t.creado_en.strftime("%Y-%m-%d %H:%M"),
            t.cerrado_en.strftime("%Y-%m-%d %H:%M") if t.cerrado_en else "",
            horas, SLA_HORAS[t.prioridad], "si" if cumplido else "no",
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
        GROUP BY estado
        ORDER BY total DESC
    """)

    por_categoria = consultar("""
        SELECT categoria, COUNT(*) AS total
        FROM tickets
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
        GROUP BY titulo
        ORDER BY veces DESC
        LIMIT 5
    """)

    escalamiento = consultar("""
        SELECT ROUND(100.0 * SUM(CASE WHEN nivel = 2 THEN 1 ELSE 0 END) / COUNT(*), 1) AS porcentaje_escalados
        FROM tickets
    """)

    return jsonify({
        "tickets_por_estado": por_estado,
        "tickets_por_categoria": por_categoria,
        "sla_por_prioridad": sla_por_prioridad,
        "top_5_problemas": problemas_repetidos,
        "escalamiento": escalamiento[0] if escalamiento else {},
    })
