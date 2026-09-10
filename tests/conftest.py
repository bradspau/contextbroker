import sys
from pathlib import Path

# Add contextbroker parent directory to Python path so broker.py can be imported
sys.path.insert(0, str(Path(__file__).parent.parent))
