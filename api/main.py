# api/main.py

from flask import Flask, jsonify, request
from sqlalchemy import text
import os
import click

from database import db 

app = Flask(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL')
BROKER_URL = os.environ.get('BROKER_URL')

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


db.init_app(app)


from models import Room, Triage, Appointment 


@app.route("/")
def read_root():
    """ Rota raiz da API """
    db_status = "desconectado"
    try:
        db.session.execute(text('SELECT 1'))
        db_status = "conectado"
    except Exception as e:
        db_status = f"erro: {str(e)}"

    return jsonify(
        servico="Agendamento & Triagem",
        status="online",
        database_url_detectada=bool(DATABASE_URL),
        broker_url_detectado=bool(BROKER_URL),
        status_db=db_status
    )



@app.route('/rooms', methods=['POST'])
def create_room():
    """ Cria uma nova sala no banco de dados. """
    data = request.json

    if not data or not 'room_name' in data or not 'room_type' in data:
        return jsonify({"erro": "Dados incompletos"}), 400

    new_room = Room(
        room_name=data['room_name'],
        room_type=data['room_type']
    )

    db.session.add(new_room)
    db.session.commit()

    return jsonify({"id": new_room.id, "room_name": new_room.room_name}), 201

@app.route('/rooms', methods=['GET'])
def get_rooms():
    """ Lista todas as salas cadastradas. """
    rooms = Room.query.all()

    rooms_list = []
    for room in rooms:
        rooms_list.append({
            "id": room.id,
            "room_name": room.room_name,
            "room_type": room.room_type
        })

    return jsonify(rooms_list), 200


@app.cli.command('init-db')
def init_db_command():
    """Cria as tabelas do banco de dados (definidas em models.py)."""
    with app.app_context():
        db.create_all()
    print('Banco de dados inicializado e tabelas criadas!')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)