import sys
from pathlib import Path

# 将项目根目录（ssm/）加入 sys.path，使 cloud.esp32 可被导入
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
