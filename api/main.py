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
# ENDPOINTS DE /triage
# ==========================================================
@app.route('/triage', methods=['POST'])
def create_triage():
    """
    Cria uma nova triagem aplicando o Protocolo de Manchester.
    Espera: patient_id, symptoms (lista), appointment_id (opcional)
    """
    data = request.json
    
    if not data or 'patient_id' not in data or 'symptoms' not in data:
        log_event("triage_create_failed", {"motivo": "dados incompletos"}, level="error")
        return jsonify({"erro": "Campos obrigatórios ausentes (patient_id, symptoms)"}), 400
    
    patient_id = data['patient_id']
    symptoms = data.get('symptoms', [])
    appointment_id = data.get('appointment_id')
    
    # Aplicar lógica do Protocolo de Manchester
    manchester_score = calculate_manchester_score(symptoms)
    
    # Criar registro de triagem
    new_triage = Triage(
        patient_id=patient_id,
        manchester_score=manchester_score,
        appointment_id=appointment_id
    )
    
    db.session.add(new_triage)
    db.session.commit()
    
    # Publicar evento de triagem realizada
    publish_event("TriageScoreAssigned", {
        "id": new_triage.id,
        "patient_id": new_triage.patient_id,
        "manchester_level": new_triage.manchester_score,
        "symptoms": symptoms,
        "assigned_at": new_triage.timestamp.isoformat(),
        "appointment_id": appointment_id
    })
    
    log_event("triage_created", {
        "triage_id": new_triage.id,
        "patient_id": new_triage.patient_id,
        "manchester_score": new_triage.manchester_score,
        "symptoms": symptoms
    })
    
    return jsonify({
        "id": new_triage.id,
        "patient_id": new_triage.patient_id,
        "manchester_score": new_triage.manchester_score,
        "priority": get_priority_name(manchester_score),
        "timestamp": new_triage.timestamp.isoformat()
    }), 201


@app.route('/triage', methods=['GET'])
def list_triages():
    """Lista triagens com filtros opcionais por paciente ou appointment."""
    query = Triage.query
    
    patient_id = request.args.get('patient_id')
    appointment_id = request.args.get('appointment_id')
    
    if patient_id:
        query = query.filter_by(patient_id=patient_id)
    if appointment_id:
        query = query.filter_by(appointment_id=appointment_id)
    
    # Ordenar do mais recente para o mais antigo
    triages = query.order_by(Triage.timestamp.desc()).all()
    
    result = [
        {
            "id": t.id,
            "patient_id": t.patient_id,
            "manchester_score": t.manchester_score,
            "priority": get_priority_name(t.manchester_score),
            "timestamp": t.timestamp.isoformat(),
            "appointment_id": t.appointment_id
        }
        for t in triages
    ]
    
    log_event("triages_listed", {"total": len(result)})
    return jsonify(result), 200


@app.route('/triage/<int:triage_id>', methods=['GET'])
def get_triage(triage_id):
    """Retorna os detalhes de uma triagem específica."""
    triage = Triage.query.get(triage_id)
    
    if not triage:
        log_event("triage_get_failed", {"motivo": "não encontrada", "id": triage_id}, level="error")
        return jsonify({"erro": "Triagem não encontrada"}), 404
    
    log_event("triage_retrieved", {"triage_id": triage_id})
    
    return jsonify({
        "id": triage.id,
        "patient_id": triage.patient_id,
        "manchester_score": triage.manchester_score,
        "priority": get_priority_name(triage.manchester_score),
        "timestamp": triage.timestamp.isoformat(),
        "appointment_id": triage.appointment_id
    }), 200


# ==========================================================
# FUNÇÕES AUXILIARES - PROTOCOLO DE MANCHESTER
# ==========================================================
def calculate_manchester_score(symptoms):
    """
    Aplica lógica simplificada do Protocolo de Manchester.
    Retorna um score de 1 (emergência) a 5 (não urgente).
    
    Níveis:
    1 - EMERGENTE (vermelho): risco imediato de vida
    2 - MUITO URGENTE (laranja): risco de vida potencial
    3 - URGENTE (amarelo): condições que podem piorar
    4 - POUCO URGENTE (verde): problemas menos graves
    5 - NÃO URGENTE (azul): condições crônicas ou menores
    """
    if not symptoms or len(symptoms) == 0:
        return 5  # Sem sintomas = não urgente
    
    symptoms_lower = [s.lower() for s in symptoms]
    
    # Palavras-chave para cada nível de prioridade
    emergency_keywords = [
        'parada', 'cardíaca', 'respiratória', 'inconsciência', 'inconsciente',
        'convulsão', 'hemorragia', 'severa', 'choque', 'trauma', 'grave'
    ]
    
    very_urgent_keywords = [
        'dor no peito', 'falta de ar', 'dificuldade respirar', 'confusão mental',
        'alteração consciência', 'sangramento', 'fratura exposta', 'queimadura grave'
    ]
    
    urgent_keywords = [
        'febre alta', 'dor intensa', 'vômito', 'diarreia', 'desidratação',
        'tontura', 'fratura', 'luxação', 'corte profundo'
    ]
    
    less_urgent_keywords = [
        'dor moderada', 'febre', 'tosse', 'resfriado', 'dor de garganta',
        'náusea', 'dor de ouvido', 'pequeno corte'
    ]
    
    # Verificar por palavras-chave (da mais grave para menos grave)
    for symptom in symptoms_lower:
        # Nível 1 - EMERGENTE
        if any(keyword in symptom for keyword in emergency_keywords):
            return 1
    
    for symptom in symptoms_lower:
        # Nível 2 - MUITO URGENTE
        if any(keyword in symptom for keyword in very_urgent_keywords):
            return 2
    
    for symptom in symptoms_lower:
        # Nível 3 - URGENTE
        if any(keyword in symptom for keyword in urgent_keywords):
            return 3
    
    for symptom in symptoms_lower:
        # Nível 4 - POUCO URGENTE
        if any(keyword in symptom for keyword in less_urgent_keywords):
            return 4
    
    # Nível 5 - NÃO URGENTE (default)
    return 5


def get_priority_name(score):
    """Retorna o nome da prioridade baseado no score de Manchester."""
    priority_map = {
        1: "Emergente (Vermelho)",
        2: "Muito Urgente (Laranja)",
        3: "Urgente (Amarelo)",
        4: "Pouco Urgente (Verde)",
        5: "Não Urgente (Azul)"
    }
    return priority_map.get(score, "Desconhecido")


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