# api/main.py

from flask import Flask, jsonify, request
from sqlalchemy import text
import os
import click
from datetime import datetime
import pika
import json

from database import db
from models import Room, Triage, Appointment
from logger import log_event   #módulo de logging estruturado


# ==========================================================
# Configuração do Flask e Banco
# ==========================================================
app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
BROKER_URL = os.environ.get('BROKER_URL')

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)


# ==========================================================
# Publicação de Eventos no RabbitMQ
# ==========================================================
def publish_event(event_type, data):
    """Publica um evento JSON no RabbitMQ e loga a ação."""
    if not BROKER_URL:
        log_event("rabbitmq_config_missing", {"BROKER_URL": None}, level="error")
        return

    try:
        connection = pika.BlockingConnection(pika.URLParameters(BROKER_URL))
        channel = connection.channel()
        channel.queue_declare(queue='events', durable=True)

        payload = {
            "event_type": event_type,
            "data": data
        }

        channel.basic_publish(
            exchange='',
            routing_key='events',
            body=json.dumps(payload),
            properties=pika.BasicProperties(delivery_mode=2)
        )

        connection.close()
        log_event("rabbitmq_event_published", {"event_type": event_type, "data": data})

    except Exception as e:
        log_event("rabbitmq_publish_failed", {"erro": str(e)}, level="error")


# ==========================================================
# Rota Raiz
# ==========================================================
@app.route("/")
def read_root():
    """Rota raiz da API."""
    db_status = "desconectado"
    try:
        db.session.execute(text('SELECT 1'))
        db_status = "conectado"
    except Exception as e:
        db_status = f"erro: {str(e)}"

    status_info = {
        "servico": "Agendamento & Triagem",
        "status": "online",
        "database_url_detectada": bool(DATABASE_URL),
        "broker_url_detectado": bool(BROKER_URL),
        "status_db": db_status
    }
    log_event("health_check", status_info)
    return jsonify(status_info)


# ==========================================================
# ENDPOINTS DE /rooms
# ==========================================================
@app.route('/rooms', methods=['POST'])
def create_room():
    """Cria uma nova sala no banco de dados."""
    data = request.json

    if not data or not 'room_name' in data or not 'room_type' in data:
        log_event("room_create_failed", {"motivo": "dados incompletos"}, level="error")
        return jsonify({"erro": "Dados incompletos"}), 400

    new_room = Room(
        room_name=data['room_name'],
        room_type=data['room_type']
    )

    db.session.add(new_room)
    db.session.commit()

    log_event("room_created", {"id": new_room.id, "room_name": new_room.room_name})
    return jsonify({"id": new_room.id, "room_name": new_room.room_name}), 201


@app.route('/rooms', methods=['GET'])
def get_rooms():
    """Lista todas as salas cadastradas."""
    rooms = Room.query.all()
    rooms_list = [{"id": r.id, "room_name": r.room_name, "room_type": r.room_type} for r in rooms]
    log_event("rooms_listed", {"total": len(rooms_list)})
    return jsonify(rooms_list), 200


# ==========================================================
# ENDPOINTS DE /appointments
# ==========================================================
@app.route('/appointments', methods=['POST'])
def create_appointment():
    """Cria um novo agendamento (com validação de disponibilidade)."""
    data = request.json
    required_fields = ['patient_id', 'staff_id', 'room_id', 'start_time', 'end_time']

    if not data or not all(field in data for field in required_fields):
        log_event("appointment_create_failed", {"motivo": "campos obrigatórios ausentes"}, level="error")
        return jsonify({"erro": "Campos obrigatórios ausentes"}), 400

    try:
        start_time = datetime.fromisoformat(data['start_time'])
        end_time = datetime.fromisoformat(data['end_time'])
    except ValueError:
        log_event("appointment_create_failed", {"motivo": "data inválida"}, level="error")
        return jsonify({"erro": "Formato de data inválido. Use ISO 8601 (YYYY-MM-DDTHH:MM:SS)"}), 400

    conflicts = Appointment.query.filter(
        Appointment.room_id == data['room_id'],
        Appointment.start_time < end_time,
        Appointment.end_time > start_time
    ).all()

    if conflicts:
        log_event("appointment_conflict", {"room_id": data['room_id'], "start_time": data['start_time']}, level="error")
        return jsonify({"erro": "A sala já está ocupada neste horário"}), 409

    new_appointment = Appointment(
        patient_id=data['patient_id'],
        staff_id=data['staff_id'],
        room_id=data['room_id'],
        start_time=start_time,
        end_time=end_time,
        status='agendado'
    )

    db.session.add(new_appointment)
    db.session.commit()

    publish_event("AppointmentBooked", {
        "appointment_id": new_appointment.id,
        "patient_id": new_appointment.patient_id,
        "room_id": new_appointment.room_id,
        "start_time": data['start_time']
    })

    log_event("appointment_created", {
        "appointment_id": new_appointment.id,
        "patient_id": new_appointment.patient_id,
        "staff_id": new_appointment.staff_id,
        "room_id": new_appointment.room_id,
        "start_time": new_appointment.start_time.isoformat()
    })

    return jsonify({
        "id": new_appointment.id,
        "status": new_appointment.status
    }), 201


@app.route('/appointments', methods=['GET'])
def list_appointments():
    """Lista agendamentos (com filtros opcionais por data, médico ou sala)."""
    query = Appointment.query

    date_filter = request.args.get('date')
    staff_id = request.args.get('staff_id')
    room_id = request.args.get('room_id')

    if date_filter:
        try:
            date_obj = datetime.fromisoformat(date_filter)
            query = query.filter(
                Appointment.start_time >= date_obj,
                Appointment.start_time < date_obj.replace(hour=23, minute=59, second=59)
            )
        except ValueError:
            log_event("appointment_list_failed", {"motivo": "data inválida"}, level="error")
            return jsonify({"erro": "Formato de data inválido"}), 400

    if staff_id:
        query = query.filter_by(staff_id=staff_id)
    if room_id:
        query = query.filter_by(room_id=room_id)

    appointments = query.all()

    result = [
        {
            "id": a.id,
            "patient_id": a.patient_id,
            "staff_id": a.staff_id,
            "room_id": a.room_id,
            "start_time": a.start_time.isoformat(),
            "end_time": a.end_time.isoformat(),
            "status": a.status
        }
        for a in appointments
    ]

    log_event("appointments_listed", {"total": len(result)})
    return jsonify(result), 200


@app.route('/appointments/<int:appointment_id>', methods=['PUT'])
def update_appointment(appointment_id):
    """Atualiza o status ou horário de um agendamento."""
    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        log_event("appointment_update_failed", {"motivo": "não encontrado", "id": appointment_id}, level="error")
        return jsonify({"erro": "Agendamento não encontrado"}), 404

    data = request.json
    updated = False

    if 'status' in data:
        appointment.status = data['status']
        updated = True

    if 'start_time' in data and 'end_time' in data:
        try:
            new_start = datetime.fromisoformat(data['start_time'])
            new_end = datetime.fromisoformat(data['end_time'])
        except ValueError:
            log_event("appointment_update_failed", {"motivo": "data inválida"}, level="error")
            return jsonify({"erro": "Formato de data inválido"}), 400

        conflicts = Appointment.query.filter(
            Appointment.room_id == appointment.room_id,
            Appointment.id != appointment.id,
            Appointment.start_time < new_end,
            Appointment.end_time > new_start
        ).all()

        if conflicts:
            log_event("appointment_conflict_update", {"id": appointment_id}, level="error")
            return jsonify({"erro": "Conflito de horário com outro agendamento"}), 409

        appointment.start_time = new_start
        appointment.end_time = new_end
        updated = True

    if not updated:
        log_event("appointment_update_skipped", {"id": appointment_id})
        return jsonify({"erro": "Nada para atualizar"}), 400

    db.session.commit()

    publish_event("AppointmentRescheduled", {
        "appointment_id": appointment.id,
        "status": appointment.status,
        "start_time": appointment.start_time.isoformat(),
        "end_time": appointment.end_time.isoformat()
    })

    log_event("appointment_updated", {"appointment_id": appointment.id, "status": appointment.status})
    return jsonify({"mensagem": "Agendamento atualizado com sucesso"}), 200


# ==========================================================
# CLI para inicializar o banco
# ==========================================================
@app.cli.command('init-db')
def init_db_command():
    """Cria as tabelas do banco de dados."""
    with app.app_context():
        db.create_all()
    log_event("db_initialized", {"status": "ok"})
    print('Banco de dados inicializado e tabelas criadas!')


# ==========================================================
# Execução da aplicação
# ==========================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
