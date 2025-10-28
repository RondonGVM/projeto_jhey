# api/models.py

from database import db 

class Room(db.Model):
    __tablename__ = 'rooms'
    
    id = db.Column(db.Integer, primary_key=True)
    room_name = db.Column(db.String(100), nullable=False)

    room_type = db.Column(db.String(50), nullable=False)


class Triage(db.Model):
    __tablename__ = 'triages'
    
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, nullable=False) 
    manchester_score = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    
    appointment_id = db.Column(db.Integer, db.ForeignKey('appointments.id'), nullable=True)


class Appointment(db.Model):
    __tablename__ = 'appointments'
    
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, nullable=False) 
    staff_id = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    
    
    status = db.Column(db.String(50), nullable=False, default='agendado')
    
    
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    room = db.relationship('Room')
    
    
    triage = db.relationship('Triage', backref='appointment', uselist=False)