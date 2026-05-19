"""
rocm_mq.__main__ — enables ``python -m rocm_mq process-cycle ...``.

Delegates to :func:`rocm_mq.cmd_process.main` and forwards its exit code.
"""

from __future__ import annotations

import sys

from rocm_mq.cmd_process import main

if __name__ == "__main__":
    sys.exit(main())
