# app/events.py

import json
import os
import time
import uuid
import pika


# ===============================
#  Parâmetros de Topologia (ENV)
# ===============================
EXCHANGE_NAME = os.getenv("BROKER_EXCHANGE", "hospital.events")
EXCHANGE_TYPE = os.getenv("BROKER_EXCHANGE_TYPE", "topic")

# Filas padrão (podem ser sobrescritas por ENV)
QUEUE_DEBUG = os.getenv("QUEUE_DEBUG_EVENTS", "debug.events")
QUEUE_STATUS = os.getenv("QUEUE_APPOINTMENT_STATUS", "agenda.status")
QUEUE_ANALYTICS = os.getenv("QUEUE_APPOINTMENT_ANALYTICS", "agenda.analytics")

# Bindings padrão (routing keys)
BINDING_DEBUG = os.getenv("BINDING_DEBUG", "#")
BINDING_STATUS = os.getenv("BINDING_STATUS", "appointment.status.*")
BINDING_ANALYTICS = os.getenv("BINDING_ANALYTICS", "appointment.status.changed")

# Se quiser desabilitar a declaração automática das filas/bindings, defina BROKER_DECLARE_TOPOLOGY=false
DECLARE_TOPOLOGY = os.getenv("BROKER_DECLARE_TOPOLOGY", "true").lower() not in {"0", "false", "no"}


class RabbitPublisher:
    def __init__(self, url: str, exchange: str, exchange_type: str = "topic"):
        self.url = url
        self.exchange = exchange
        self.exchange_type = exchange_type
        self._connection = None
        self._channel = None

    # -------------------------------
    #  Conexão + declaração topologia
    # -------------------------------
    def _connect(self, max_retries: int = 10, backoff_sec: float = 2.0):
        params = pika.URLParameters(self.url)
        last_exc = None
        for _ in range(max_retries):
            try:
                self._connection = pika.BlockingConnection(params)
                self._channel = self._connection.channel()

                # 1) Exchange (topic, durável, sem auto-delete, não-internal)
                self._channel.exchange_declare(
                    exchange=self.exchange,
                    exchange_type=self.exchange_type,
                    durable=True,
                    auto_delete=False,
                    internal=False,
                    arguments=None,
                )

                # 2) Topologia (filas + bindings), se habilitado
                if DECLARE_TOPOLOGY:
                    self._declare_topology(self._channel)

                # 3) Publisher confirms
                self._channel.confirm_delivery()
                return
            except Exception as exc:
                last_exc = exc
                time.sleep(backoff_sec)
        raise RuntimeError(f"Falha ao conectar no RabbitMQ: {last_exc}")

    def _declare_topology(self, channel: pika.adapters.blocking_connection.BlockingChannel):
        """
        Declara filas e bindings com todos os parâmetros explícitos.
        Chamada durante a conexão, após declarar a exchange.
        """

        # 2a) Filas (duráveis, não-exclusivas, sem auto-delete)
        channel.queue_declare(
            queue=QUEUE_DEBUG,
            durable=True,      # persiste após restart
            exclusive=False,   # pode ter múltiplos consumidores
            auto_delete=False, # não apaga sozinha
            arguments=None     # aqui você poderia configurar DLQ/TTL, se quiser
        )
        channel.queue_declare(
            queue=QUEUE_STATUS,
            durable=True,
            exclusive=False,
            auto_delete=False,
            arguments=None
        )
        channel.queue_declare(
            queue=QUEUE_ANALYTICS,
            durable=True,
            exclusive=False,
            auto_delete=False,
            arguments=None
        )

        # 2b) Bindings (regras de roteamento)
        channel.queue_bind(
            queue=QUEUE_DEBUG,
            exchange=self.exchange,
            routing_key=BINDING_DEBUG,  # "#"
            arguments=None
        )
        channel.queue_bind(
            queue=QUEUE_STATUS,
            exchange=self.exchange,
            routing_key=BINDING_STATUS,  # "appointment.status.*"
            arguments=None
        )
        channel.queue_bind(
            queue=QUEUE_ANALYTICS,
            exchange=self.exchange,
            routing_key=BINDING_ANALYTICS,  # "appointment.status.changed"
            arguments=None
        )

    # -------------------------------
    #  Garantia de canal conectado
    # -------------------------------
    def _ensure_channel(self):
        if not self._connection or self._connection.is_closed:
            self._connect()
        if not self._channel or self._channel.is_closed:
            self._connect()

    # -------------------------------
    #  Publicação de mensagens
    # -------------------------------
    def publish(self, routing_key: str, payload: dict, headers: dict | None = None):
        """
        Publica na exchange configurada, com delivery_mode=2 (persistente) e publisher confirms.
        - routing_key: ex. "appointment.status.check_in", "appointment.booked"
        - payload: dict serializado para JSON
        - headers: metadados extras (X-headers)
        """
        self._ensure_channel()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        event_id = str(uuid.uuid4())
        props = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,  # 2 = persistente (disco) quando a fila é durável
            headers={
                "event-id": event_id,
                "service": os.getenv("SERVICE_NAME", "scheduling-triage"),
                **(headers or {}),
            },
        )
        try:
            self._channel.basic_publish(
                exchange=self.exchange,    # "hospital.events"
                routing_key=routing_key,   # ex. "appointment.status.changed"
                body=body,
                properties=props,
                mandatory=True,            # gera Basic.Return se não houver rota
            )
            return event_id
        except pika.exceptions.UnroutableError:
            raise RuntimeError(f"Evento sem rota para '{routing_key}'")
        except Exception as exc:
            raise RuntimeError(f"Erro publicando evento '{routing_key}': {exc}")


# ===============================
#  Singleton do Publisher
# ===============================
_publisher_singleton: RabbitPublisher | None = None

def get_publisher() -> RabbitPublisher:
    """
    Retorna uma instância única do publisher já conectada e com a topologia garantida.
    """
    global _publisher_singleton
    if _publisher_singleton is None:
        url = os.getenv("BROKER_URL", "amqp://guest:guest@broker:5672/")
        exch = EXCHANGE_NAME
        exch_type = EXCHANGE_TYPE
        _publisher_singleton = RabbitPublisher(url, exch, exch_type)
        _publisher_singleton._connect()  # falha cedo se der erro
    return _publisher_singleton
