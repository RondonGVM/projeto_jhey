# api/logger.py
import json
import logging
from datetime import datetime


# ==========================================================
# Configuração base do logger
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'  # Saída em formato puro (JSON string)
)


def log_event(event_type: str, data: dict, level: str = "info", service_name: str = "agendamento_triagem_api"):
    """
    Loga eventos estruturados no formato JSON.
    Ideal para Docker, ELK e sistemas de monitoramento.

    Args:
        event_type (str): tipo do evento (ex: appointment_created)
        data (dict): dados relevantes para o evento
        level (str): nível do log ('info' ou 'error')
        service_name (str): nome do microserviço
    """
    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "event": event_type,
        "service": service_name,
        "data": data
    }

    json_entry = json.dumps(log_entry, ensure_ascii=False)

    if level == "error":
        logging.error(json_entry)
    else:
        logging.info(json_entry)
