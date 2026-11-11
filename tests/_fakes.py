"""Fake credentials for tests.

The API key is assembled at import time so the source never holds a complete Google-key-shaped
literal: GitHub secret scanning flags one as a leaked key (alert #1) even when it is made up.
"""

# 39 characters, "AIza" + 35, the shape of a real Google API key. Not a real key.
FAKE_GOOGLE_API_KEY = "AIza" + "Sy" + "FAKE" + "0" * 29

# A newer AI Studio key shape: "AQ" + 51, 53 characters. Not a real key (#119).
FAKE_AQ_API_KEY = "AQ" + "." + "FAKE" + "x" * 46
