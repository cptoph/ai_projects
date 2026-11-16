"""WSGI entry point for the single-process Flask container."""

import atexit

from app import create_app
from dashboard import DashboardService

service = DashboardService()
app = create_app(service)
service.start()
atexit.register(service.stop)
