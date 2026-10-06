"""Refreshable keyless credentials obtained through the signed-in gcloud CLI."""
from datetime import datetime, timedelta, timezone
import shutil
import subprocess

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials


def impersonated_credentials(config):
    executable = shutil.which("gcloud.cmd") or shutil.which("gcloud")
    if not executable:
        raise RuntimeError("Google Cloud CLI is not on PATH.")

    def refresh_handler(request, scopes):
        del request, scopes
        # An explicit lifetime makes gcloud request a fresh impersonated token.
        # gcloud's user login is used only to authorize this impersonation.
        arguments = [executable, "auth", "print-access-token",
                     "--impersonate-service-account=" + config["serviceAccountEmail"],
                     "--account=" + config["provisioningAccount"],
                     "--project=" + config["projectId"], "--lifetime=3600", "--quiet"]
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RefreshError("Could not invoke gcloud for service-account impersonation.") from error
        if result.returncode or not result.stdout.strip():
            # Do not include stdout: it could contain a credential.
            raise RefreshError("Impersonation failed. Check your CLI login and Token Creator grant. " + result.stderr.strip())
        token = result.stdout.strip()
        if any(character.isspace() for character in token):
            raise RefreshError("gcloud returned an unexpected token format.")
        # Refresh before the one-hour token expires; google-auth expects naive UTC.
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=55)
        return token, expiry

    return Credentials(token=None, scopes=["https://www.googleapis.com/auth/cloud-platform"],
                       refresh_handler=refresh_handler)
