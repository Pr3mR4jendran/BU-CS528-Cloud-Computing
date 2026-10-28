"""Use the invoking gcloud session for bootstrap, VM ADC for runtime."""
from datetime import datetime, timedelta, timezone
import shutil
import subprocess

from google.auth.credentials import Credentials


class GcloudCredentials(Credentials):
    """Refresh short-lived user credentials without an ADC login or key file."""

    def refresh(self, request):
        executable = shutil.which("gcloud")
        if not executable:
            raise RuntimeError("Install gcloud and run gcloud auth login first.")
        result = subprocess.run([executable, "auth", "print-access-token", "--quiet"],
                                capture_output=True, text=True, check=False)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError("gcloud authentication failed. Run gcloud auth login.")
        self.token = result.stdout.strip()
        self.expiry = (datetime.now(timezone.utc) + timedelta(minutes=50)).replace(tzinfo=None)
