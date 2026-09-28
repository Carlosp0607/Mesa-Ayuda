import os
from datetime import datetime
from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# Conexión a la base de datos
db_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

db = SQLAlchemy(app)

# SLA: horas máximas para resolver según la prioridad
SLA_HORAS = {"alta": 4, "media": 24, "baja": 72}


class Ticket(db.Model):
    __tablename__ = "tickets"
    id = db.Column(db.Integer, primary_key=True)
    titulo = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.Text, nullable=False)
    tipo = db.Column(db.String(20), nullable=False)        # incidente | requerimiento
    categoria = db.Column(db.String(30), nullable=False)   # acceso | error | datos | rendimiento | consulta
    prioridad = db.Column(db.String(10), nullable=False)   # alta | media | baja
    estado = db.Column(db.String(20), nullable=False, default="abierto")  # abierto | en_proceso | escalado | cerrado
    nivel = db.Column(db.Integer, nullable=False, default=1)  # 1 = primer nivel, 2 = escalado
    solucion = db.Column(db.Text)
    creado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    cerrado_en = db.Column(db.DateTime)


class Articulo(db.Model):
    __tablename__ = "base_conocimiento"
    id = db.Column(db.Integer, primary_key=True)
    categoria = db.Column(db.String(30), nullable=False)
    problema = db.Column(db.String(200), nullable=False)
    solucion = db.Column(db.Text, nullable=False)


with app.app_context():
    db.create_all()


@app.route("/")
def inicio():
    return jsonify({"sistema": "Mesa de Ayuda", "estado": "funcionando"})
