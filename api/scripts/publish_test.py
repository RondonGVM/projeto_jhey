from datetime import datetime, timezone
from app.events import get_publisher

publisher = get_publisher()
payload = {
    "event": "triage.score_assigned",
    "version": 1,
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "triage": {
        "id": 999,
        "patient_id": 123,
        "manchester_level": 3,
        "symptoms": ["dor de cabeça", "náusea"],
        "assigned_at": datetime.now(timezone.utc).isoformat(),
    },
}
event_id = publisher.publish("triage.score_assigned", payload)
print("Publicado com event-id:", event_id)
