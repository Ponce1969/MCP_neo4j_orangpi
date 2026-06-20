# scripts/setup_env.py
"""Para que el agente configure el entorno localmente sin errores."""

import shutil
from pathlib import Path


def main() -> None:
    env_example = Path(".env.example")
    env_file = Path(".env")

    if not env_file.exists() and env_example.exists():
        shutil.copy(env_example, env_file)
        print("✅ Archivo .env creado desde .env.example. Por favor, rellena las variables.")
    elif env_file.exists():
        print("ℹ️ El archivo .env ya existe.")
    else:
        print("❌ No se encontró .env.example")


if __name__ == "__main__":
    main()
