"""HTTP entry point deployed as a second-generation Cloud Function."""
import threading

import functions_framework

from service import FileService, Settings

_service = None
_lock = threading.Lock()


@functions_framework.http
def serve_file(request):
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = FileService.from_settings(Settings.from_environment())
    return _service.handle(request)
