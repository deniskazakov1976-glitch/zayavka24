"""Generates static index.html in docs/ for GitHub Pages deployment."""
from pathlib import Path

def build():
    src = Path("app/templates/landing.html")
    if not src.exists():
        raise FileNotFoundError(f"{src} does not exist")
    raw = src.read_text(encoding="utf-8")
    html = raw.replace("{{BOT_USERNAME}}", "Zayavka24AI_bot")
    html = html.replace('href="/" target="_blank"', 'href="https://77.221.155.74.sslip.io/" target="_blank"')
    html = html.replace('href="/login"', 'href="https://77.221.155.74.sslip.io/login"')
    html = html.replace('href="/landing"', 'href="#"')

    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
    out = docs_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"Successfully generated {out} ({len(html)} bytes)")

if __name__ == "__main__":
    build()
