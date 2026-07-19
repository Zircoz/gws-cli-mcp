import os
import sys
from pathlib import Path

# Make server.py / app.py importable as top-level modules, matching how
# uvicorn imports them ("app:app_with_auth") in this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# app.py fails closed (raises RuntimeError) at import time if ENABLE_AUTH,
# REQUIRED_SCOPES, or GWS_PER_USER_TOKEN look misconfigured — that's the
# behavior under test, but it also means `import app` must succeed during
# test collection. Force (not merely default) known-good values here before
# any test module imports app/server: os.environ.setdefault() would be a
# no-op if a developer's or CI's ambient shell already exports a conflicting
# value (e.g. ENABLE_AUTH=1, an empty REQUIRED_SCOPES), which would fail the
# entire test suite at collection instead of just running with clean state.
# Tests that exercise the validation logic itself call the extracted
# _resolve_enable_auth / _resolve_required_scopes helpers directly rather
# than re-importing the module with different env vars.
os.environ["ENABLE_AUTH"] = "true"
os.environ.pop("REQUIRED_SCOPES", None)
os.environ.pop("GWS_PER_USER_TOKEN", None)
os.environ.setdefault("OAUTH_ISSUER", "https://test-idp.example.com/")
os.environ.setdefault("OAUTH_AUDIENCE", "https://mcp.test.example.com")


class FakeProcess:
    """Stand-in for asyncio.subprocess.Process, returned by a monkeypatched
    asyncio.create_subprocess_exec so tests never spawn a real `gws`."""

    def __init__(self, stdout: bytes = b"ok", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr
