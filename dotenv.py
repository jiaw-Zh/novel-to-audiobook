"""从 .env 文件加载环境变量"""
import os
from pathlib import Path


def load_dotenv(env_path: str = ".env"):
    """加载 .env 文件到环境变量（不覆盖已有变量）"""
    path = Path(env_path)
    if not path.exists():
        return

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # 跳过注释和空行
            if not line or line.startswith("#"):
                continue
            # 解析 KEY=VALUE
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # 不覆盖已有环境变量
                if key and key not in os.environ:
                    os.environ[key] = value
