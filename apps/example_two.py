"""Тестовый модуль 2: создаёт тестовый JSON-файл в общей папке data/.

Показывает, как модуль может пользоваться общей папкой данных проекта.
"""

import json
import time
from datetime import datetime
from pathlib import Path

# Общая папка данных проекта (расположена на уровень выше папки apps/)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def run():
    """Главная функция модуля. Вызывается лаунчером."""
    print("=== example_two запущен ===")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "module": "example_two",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "message": "Привет из модуля example_two!",
        "values": [1, 2, 3, 4, 5],
    }

    target = DATA_DIR / "example_two_output.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    time.sleep(0.3)
    print(f"  Файл создан: {target}")
    print("=== example_two завершён ===")


if __name__ == "__main__":
    run()
