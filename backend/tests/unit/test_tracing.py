from unittest.mock import patch

from app.core.observability.tracing import configure_tracing
from app.settings import Settings


def test_tracing_disabled_creates_no_exporter() -> None:
    settings = Settings(_env_file=None, tracing_enabled=False)  # type: ignore[call-arg]
    with patch("app.core.observability.tracing.OTLPSpanExporter") as exporter:
        assert configure_tracing(settings) is None
    exporter.assert_not_called()
