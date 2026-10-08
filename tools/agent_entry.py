"""Register either unchanged upstream callbacks or the isolated HIF callbacks."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / 'tools'))
from hif_app import initialize_defaults
initialize_defaults(ROOT)
if '--hif-only' in sys.argv:
    sys.path.insert(0, str(ROOT / 'extensions/hif/agent'))
    import hif
else:
    sys.path.insert(0, str(ROOT / 'upstream/agent'))
    import custom  # upstream registrations, unchanged
from maa.toolkit import Toolkit
from maa.agent.agent_server import AgentServer
from utils import logger

if __name__ == '__main__':
    Toolkit.init_option(str(ROOT))
    AgentServer.start_up(sys.argv[-1])
    logger.info('独立 HIF Agent 已启动')
    try:
        AgentServer.join()
    finally:
        AgentServer.shut_down()
