
from flask import Flask, jsonify
import os

app = Flask(__name__)


DATABASE_URL = os.environ.get('DATABASE_URL')
BROKER_URL = os.environ.get('BROKER_URL')

@app.route("/")
def read_root():
    """ Rota raiz da API """
    return jsonify(
        servico="Agendamento & Triagem",
        status="online",
        database_url_detectada=bool(DATABASE_URL), 
        broker_url_detectado=bool(BROKER_URL)      
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)