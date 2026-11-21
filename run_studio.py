"""
run_studio.py — Windows-safe 1-click launcher for AI Shorts Automator Studio Pro.
Opens default web browser and starts FastAPI Uvicorn server without batch encoding issues.
"""
import os
import sys
import time
import threading
import webbrowser

# Ensure console encoding on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure working directory is project root
project_root = os.path.dirname(os.path.abspath(__file__))
os.chdir(project_root)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from utils import register_cuda_dll_dirs, setup_console_encoding

setup_console_encoding()
register_cuda_dll_dirs()

from state import app_state


def open_browser_delayed(url: str, delay: float = 1.2):
    """Opens default browser after a short delay to let server start listening."""
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[!] Не вдалося автоматично відкрити браузер: {e}")
        print(f"[*] Будь ласка, відкрийте посилання вручну: {url}")


def main():
    print("======================================================================")
    print("⚡  AI VIDEO MOMENTS ANALYZER STUDIO")
    print("    Миттєве зчитування відео, виявлення найкращих періодів часу та хуків")
    print("======================================================================")
    
    # Probe hardware (cached in AppState)
    try:
        caps = app_state.get_gpu_capabilities()
        if caps.has_nvenc:
            gpu_str = caps.nvenc_device_name or "NVIDIA GeForce GPU"
            print(f"[*] 🟢 Апаратне прискорення: {gpu_str} (NVENC 1080x1920)")
        else:
            print("[*] 💻 Режим рендерингу: Процесор CPU (libx264)")
    except Exception:
        pass

    url = "http://127.0.0.1:8000"

    # Студія вже запущена (наприклад, двічі клікнули ярлик): просто відкриваємо її в браузері
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", 8000)) == 0:
            print("[*] Студія вже працює, відкриваю її в браузері.")
            webbrowser.open(url)
            return
    print(f"[*] 🚀 Запуск веб-студії: {url}")
    print("[*] 🌐 Браузер відкриється автоматично...")
    print("======================================================================")
    print("[i] Для завершення роботи закрийте це вікно або натисніть Ctrl+C")
    print("")

    # Start browser opener in background thread
    threading.Thread(target=open_browser_delayed, args=(url,), daemon=True).start()

    # Start Uvicorn
    import uvicorn
    uvicorn.run("backend.server:app", host="127.0.0.1", port=8000, reload=False, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[*] Роботу студії успішно завершено.")
    except Exception as e:
        print(f"\n[!] Виникла помилка: {e}")
        input("\nНатисніть Enter для виходу...")
