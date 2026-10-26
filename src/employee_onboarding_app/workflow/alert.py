import json
import logging
from datetime import UTC, datetime
from typing import Any

from employee_onboarding_app.workflow.base import AlertService

logger = logging.getLogger("employee_onboarding_app.alerts")


class StructuredLogAdapter(AlertService):
    """Production structured logging alert adapter.

    Emits standardized JSON payload logs suitable for ingestion by Datadog, CloudWatch,
    or Grafana Loki. Can be easily substituted with SlackAdapter or SNSAdapter.
    """

    def __init__(self, service_name: str = "onboarding-service") -> None:
        self.service_name = service_name

    def _format_payload(
        self,
        level: str,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "service": self.service_name,
            "level": level,
            "message": message,
            "context": context or {},
        }
        return json.dumps(payload, default=str)

    def info(self, message: str, context: dict[str, Any] | None = None) -> None:
        formatted = self._format_payload("INFO", message, context)
        logger.info(formatted)

    def error(self, message: str, context: dict[str, Any] | None = None) -> None:
        formatted = self._format_payload("ERROR", message, context)
        logger.error(formatted)


# Default shared instance
default_alert_service = StructuredLogAdapter()
