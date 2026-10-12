"""Test-only composition defaults.

Production starts empty. The legacy portal scenarios intentionally opt into the
reference dataset so they test behavior against stable identifiers without making
those invented records appear in a real installation.
"""

import os


os.environ.setdefault("NETCI_DEMO_DATA", "true")
