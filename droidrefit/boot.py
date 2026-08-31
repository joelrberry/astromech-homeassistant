# Runs first on every boot. Keep minimal — the app lives in /main.py -> app.
import sys
if "/lib" not in sys.path:
    sys.path.append("/lib")
