"""Write the API's OpenAPI document to apps/web/openapi.json (input for the typed TS client)."""

import json
import sys
from pathlib import Path

from agentos.main import app

out = Path(sys.argv[1] if len(sys.argv) > 1 else "apps/web/openapi.json")
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
print(f"wrote {out} ({len(app.openapi()['paths'])} paths)")
