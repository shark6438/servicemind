import os
import subprocess
import sys
from pathlib import Path


def test_dotenv_disable_also_disables_pydantic_settings_file_loading():
    root = Path(__file__).resolve().parents[2]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "PYTHONPATH": str(root / "src"),
        "PYTHON_DOTENV_DISABLED": "1",
        "USE_FAKE_MODEL": "true",
        "DEFAULT_MODEL": "fake",
    }
    code = """
import runpy
ns = runpy.run_path('src/core/settings.py')
assert ns['Settings'].model_config.get('env_file') is None
settings = ns['settings']
assert settings.DEEPSEEK_API_KEY is None
assert settings.OPENAI_API_KEY is None
assert settings.COMPATIBLE_API_KEY is None
print('isolated settings: PASS')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=root, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated settings: PASS"
