# api/main.py

from flask import Flask, jsonify, request, current_app
from sqlalchemy import text
import os
import click
from datetime import datetime, timezone

from database import db
from models import Room, Triage, Appointment
from logger import log_event   # módulo de logging estruturado
from app.events import get_publisher


# ==========================================================
# Configuração do Flask e Banco
# ==========================================================
app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
BROKER_URL = os.environ.get('BROKER_URL')

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# Inicializa o publisher do RabbitMQ na subida (declara exchange e testa conexão)
try:
    get_publisher()
    app.logger.info("RabbitMQ publisher pronto.")
except Exception as exc:
    app.logger.error(f"Falha ao iniciar publisher RabbitMQ: {exc}")


# ==========================================================
# Publicação de Eventos via exchange 'hospital.events' (topic)
# ==========================================================
ROUTING_KEYS = {
    # já existentes
    "AppointmentBooked": "appointment.booked",
    "AppointmentRescheduled": "appointment.rescheduled",
    "TriageScoreAssigned": "triage.score_assigned",

    # novos (mudança de status)
    "AppointmentCheckedIn": "appointment.status.check_in",
    "AppointmentCompleted": "appointment.status.completed",
    "AppointmentCanceled": "appointment.status.canceled",
    "AppointmentStatusChanged": "appointment.status.changed",
}

def publish_event(event_type: str, data: dict):
    """
    Publica evento no RabbitMQ via exchange 'hospital.events' (topic).
    - event_type: pode ser uma das chaves do ROUTING_KEYS ou já uma routing key direta.
    - data: dicionário com o corpo específico (appointment/triage).
    """
    routing_key = ROUTING_KEYS.get(event_type, event_type)

    payload = {
        "event": routing_key,
        "version": 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if routing_key.startswith("appointment.") and not routing_key.startswith("appointment.status"):
        # eventos de agendamento (booked/rescheduled)
        payload["appointment"] = data
    elif routing_key.startswith("appointment.status"):
        # eventos de status
        payload["status_change"] = data
    elif routing_key.startswith("triage."):
        payload["triage"] = data
    else:
        payload["data"] = data  # fallback

    try:
        publisher = get_publisher()
        event_id = publisher.publish(routing_key, payload)
        log_event("rabbitmq_event_published", {"routing_key": routing_key, "event_id": event_id})
        return event_id
    except Exception as e:
        log_event("rabbitmq_publish_failed", {"routing_key": routing_key, "erro": str(e)}, level="error")
        return None


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

    if not data or 'room_name' not in data or 'room_type' not in data:
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
        "id": new_appointment.id,
        "patient_id": new_appointment.patient_id,
        "staff_id": new_appointment.staff_id,
        "room_id": new_appointment.room_id,
        "scheduled_at": new_appointment.start_time.isoformat(),
        "end_at": new_appointment.end_time.isoformat(),
        "status": new_appointment.status,
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
    """Atualiza o status ou horário de um agendamento e publica eventos adequados."""
    appointment = Appointment.query.get(appointment_id)
    if not appointment:
        log_event("appointment_update_failed", {"motivo": "não encontrado", "id": appointment_id}, level="error")
        return jsonify({"erro": "Agendamento não encontrado"}), 404

    data = request.json or {}
    updated = False

    # Capturar estado anterior para detecção de mudança
    old_status = appointment.status
    status_changed = False
    time_changed = False

    # Validação/atualização de status
    if 'status' in data:
        novo_status = data['status']
        allowed = {"agendado", "check-in", "finalizado", "cancelado"}
        if novo_status not in allowed:
            log_event("appointment_update_failed", {
                "motivo": "status inválido",
                "recebido": novo_status,
                "permitidos": sorted(list(allowed))
            }, level="error")
            return jsonify({"erro": "status inválido", "permitidos": sorted(list(allowed))}), 400

        if novo_status != appointment.status:
            appointment.status = novo_status
            status_changed = True
            updated = True

    # Validação/atualização de horário (aqui mantém a regra atual: só troca se vier start e end juntos)
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

        if new_start != appointment.start_time or new_end != appointment.end_time:
            appointment.start_time = new_start
            appointment.end_time = new_end
            time_changed = True
            updated = True

    if not updated:
        log_event("appointment_update_skipped", {"id": appointment_id})
        return jsonify({"erro": "Nada para atualizar"}), 400

    # Commit primeiro, eventos depois
    db.session.commit()

    # 1) Se só mudou horário (e não status), publica reschedule
    if time_changed and not status_changed:
        publish_event("AppointmentRescheduled", {
            "id": appointment.id,
            "status": appointment.status,
            "scheduled_at": appointment.start_time.isoformat(),
            "end_at": appointment.end_time.isoformat(),
        })

    # 2) Se mudou status, publica específicos + genérico
    if status_changed:
        new_status = appointment.status

        # Específico por status
        if new_status == "check-in":
            publish_event("AppointmentCheckedIn", {
                "appointment_id": appointment.id,
                "old_status": old_status,
                "new_status": new_status
            })
        elif new_status == "finalizado":
            publish_event("AppointmentCompleted", {
                "appointment_id": appointment.id,
                "old_status": old_status,
                "new_status": new_status
            })
        elif new_status == "cancelado":
            publish_event("AppointmentCanceled", {
                "appointment_id": appointment.id,
                "old_status": old_status,
                "new_status": new_status
            })
        # "agendado" normalmente não dispara específico (voltar ao agendado). Mantemos só o genérico.

        # Genérico - sempre que o status muda
        publish_event("AppointmentStatusChanged", {
            "appointment_id": appointment.id,
            "old_status": old_status,
            "new_status": new_status
        })

    log_event("appointment_updated", {
        "appointment_id": appointment.id,
        "status": appointment.status,
        "time_changed": time_changed,
        "status_changed": status_changed
    })

    return jsonify({
        "id": appointment.id,
        "status": appointment.status,
        "start_time": appointment.start_time.isoformat(),
        "end_time": appointment.end_time.isoformat()
    }), 200


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
