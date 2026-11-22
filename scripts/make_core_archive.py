import os
import zipfile
from pathlib import Path

def create_archive():
    root = Path(__file__).resolve().parent.parent
    zip_path = root / "zayavka24_core_project.zip"

    include_dirs = ["app", "companies", "deploy", "docs", "scripts", "tests"]
    include_files = [
        "AI_PROJECT_CONTEXT.md",
        "README.md",
        "TZ_hardening.md",
        "TZ_next_features.md",
        "main.py",
        "requirements.txt",
        "Dockerfile",
        "docker-compose.yml",
        "pytest.ini",
        ".env.example",
        "start.bat",
        ".gitignore",
    ]

    exclude_exts = {".pyc", ".pyo", ".log", ".tmp", ".db", ".sqlite3", ".mp4", ".wav", ".mp3", ".zip"}
    exclude_dirs = {
        "__pycache__",
        ".venv",
        ".git",
        ".pytest_cache",
        "data",
        "backups",
        "video_explainer",
        "video_explainer_v2",
        "graphify-out",
        ".pdca",
        "tools",
        "playbooks",
    }

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in include_files:
            fp = root / f
            if fp.exists():
                zf.write(fp, arcname=f)
                count += 1
                print(f"[+] {f}")

        for d in include_dirs:
            dp = root / d
            if not dp.exists():
                continue
            for current_root, dirs, files in os.walk(dp):
                dirs[:] = [sub for sub in dirs if sub not in exclude_dirs]
                for file in sorted(files):
                    if any(file.endswith(ext) for ext in exclude_exts):
                        continue
                    file_path = Path(current_root) / file
                    rel_path = file_path.relative_to(root)
                    zf.write(file_path, arcname=str(rel_path).replace("\\", "/"))
                    count += 1
                    print(f"[+] {rel_path}")

    size_kb = zip_path.stat().st_size / 1024
    print(f"\n[OK] Создан архив: {zip_path.name}")
    print(f"Файлов внутри: {count}")
    print(f"Размер: {size_kb:.1f} KB ({zip_path.stat().st_size} байт)")
    return zip_path

if __name__ == "__main__":
    create_archive()
