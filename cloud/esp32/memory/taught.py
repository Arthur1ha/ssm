"""Compatibility alias for the lamp-owned taught memory implementation.

Both module paths share one lock and remain patchable by existing callers.
"""

import sys

from cloud.agents.lamp.memory import taught as _implementation

sys.modules[__name__] = _implementation
