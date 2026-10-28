#!/usr/bin/env python3
"""
Sincroniza las guías de una carpeta de Google Drive al repo de Mintlify.

Qué hace:
  1. Lista los Google Docs de la carpeta DRIVE_FOLDER_ID (ordenados por nombre).
  2. Los exporta a markdown con la API de Drive.
  3. Saca las imágenes embebidas (base64) a archivos en IMAGES_DIR.
  4. Escribe un .mdx por guía en GUIAS_DIR.
  5. Actualiza el grupo NAV_GROUP de la navegación en docs.json.

GUIAS_DIR e IMAGES_DIR los maneja este script: se borran y se regeneran en cada
corrida. Lo que edites a mano ahí se pierde; las guías se editan en Drive.

Uso:
  python scripts/sync_drive.py                 # desde Drive (usa las variables de entorno)
  python scripts/sync_drive.py --local carpeta # desde .md locales, para probar sin Drive
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

GUIAS_DIR = os.environ.get("GUIAS_DIR", "guias").strip("/")
IMAGES_DIR = os.environ.get("IMAGES_DIR", "images/guias").strip("/")
DOCS_JSON = os.environ.get("DOCS_JSON", "docs.json")
NAV_GROUP = os.environ.get("NAV_GROUP", "Guías")

GOOGLE_DOC = "application/vnd.google-apps.document"
EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
}


@dataclass
class Source:
    name: str      # nombre del archivo en Drive: define el slug y el orden
    markdown: str  # export de Google Docs a markdown


# --------------------------------------------------------------------------- Drive

def drive_service():
    """Credenciales, en orden: token de Workload Identity (sin llaves) > llave JSON > ADC."""
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    token = os.environ.get("GOOGLE_ACCESS_TOKEN", "").strip()
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if token:
        from google.oauth2.credentials import Credentials

        creds = Credentials(token)
    elif raw:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        try:
            creds, _ = google.auth.default(scopes=scopes)
        except DefaultCredentialsError:
            sys.exit("No hay credenciales de Google: falta GOOGLE_ACCESS_TOKEN o GOOGLE_SERVICE_ACCOUNT_JSON.")
    return build("drive", "v3", credentials=creds, cache_discovery=False), creds


def list_docs(service, folder_id: str) -> list[dict]:
    q = f"'{folder_id}' in parents and mimeType = '{GOOGLE_DOC}' and trashed = false"
    files, token = [], None
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name)",
            orderBy="name",
            pageSize=100,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            return files


def export_markdown(service, creds, file_id: str) -> str:
    from googleapiclient.errors import HttpError

    try:
        data = service.files().export(fileId=file_id, mimeType="text/markdown").execute()
    except HttpError as e:
        if "exportSizeLimitExceeded" not in str(e):
            raise
        # files.export tiene tope de 10 MB; los docs con muchas capturas se bajan por exportLinks
        from google.auth.transport.requests import AuthorizedSession

        meta = service.files().get(fileId=file_id, fields="exportLinks", supportsAllDrives=True).execute()
        resp = AuthorizedSession(creds).get(meta["exportLinks"]["text/markdown"], timeout=300)
        resp.raise_for_status()
        data = resp.content
    return data.decode("utf-8") if isinstance(data, bytes) else data


def sources_from_drive(folder_id: str) -> list[Source]:
    service, creds = drive_service()
    docs = list_docs(service, folder_id)
    if not docs:
        # Mejor fallar que borrar todas las guías publicadas por un problema de permisos
        sys.exit(
            f"No encontré Google Docs en la carpeta {folder_id}. "
            "¿La compartiste con el correo de la service account?"
        )
    sources = []
    for doc in docs:
        print(f"  ↓ {doc['name']}")
        sources.append(Source(doc["name"], export_markdown(service, creds, doc["id"])))
    return sources


def sources_from_dir(path: str) -> list[Source]:
    files = sorted(p for p in Path(path).iterdir() if p.suffix.lower() in (".md", ".markdown", ".txt"))
    return [Source(p.name, p.read_text(encoding="utf-8")) for p in files]


# ----------------------------------------------------------------------- Conversión

EXT_RE = re.compile(r"\.(md|mdx|markdown|txt|docx?)$", re.I)
ORDER_PREFIX_RE = re.compile(r"^\d+[\s._-]*")


def strip_name(name: str) -> str:
    return ORDER_PREFIX_RE.sub("", EXT_RE.sub("", name.strip()))


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", strip_name(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower() or "guia"


def humanize(name: str) -> str:
    s = re.sub(r"[_-]+", " ", strip_name(name)).strip()
    return s[:1].upper() + s[1:] if s else "Guía"


def clean_inline(text: str) -> str:
    text = re.sub(r"^\s*#+\s*", "", text)
    text = re.sub(r"(\*\*|__|\*|_)(.+?)\1", r"\2", text)
    text = re.sub(r"\\(.)", r"\1", text)
    return text.strip()


REF_DEF_RE = re.compile(r"^\[([^\]]+)\]:\s*<?data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=]+)>?[ \t]*$", re.M)
REF_USE_RE = re.compile(r"!\[([^\]]*)\]\[([^\]]+)\]")
INLINE_DATA_RE = re.compile(r"!\[([^\]]*)\]\(<?data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=]+)>?\)")


def extract_images(md: str, slug: str, out_dir: Path) -> str:
    """Saca las imágenes base64 que mete el export de Google a archivos."""
    web_base = f"/{IMAGES_DIR}/{slug}"
    refs: dict[str, str] = {}

    def save(name: str, mime: str, b64: str) -> str:
        fname = f"{name}.{EXT_BY_MIME.get(mime.lower(), 'png')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / fname).write_bytes(base64.b64decode(b64))
        return f"{web_base}/{fname}"

    def on_def(m: re.Match) -> str:
        label = m.group(1)
        name = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or f"imagen-{len(refs) + 1}"
        refs[label.lower()] = save(name, m.group(2), m.group(3))
        return ""

    md = REF_DEF_RE.sub(on_def, md)
    md = REF_USE_RE.sub(
        lambda m: f"![{m.group(1)}]({refs[m.group(2).lower()]})" if m.group(2).lower() in refs else m.group(0),
        md,
    )

    counter = iter(range(1, 10_000))
    md = INLINE_DATA_RE.sub(
        lambda m: f"![{m.group(1)}]({save(f'inline-{next(counter)}', m.group(2), m.group(3))})",
        md,
    )
    return md


def split_title(md: str, fallback: str) -> tuple[str, str]:
    """La primera línea del doc es el título de la página (si parece un título)."""
    lines = md.split("\n")
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        is_block = s.startswith(("!", "|", ">", "```", "- ", "* ", "+ ")) or re.match(r"^\d+[.)]\s", s)
        if is_block or len(s) > 150:
            return fallback, md
        title = clean_inline(s)
        return (title or fallback), "\n".join(lines[i + 1:])
    return fallback, md


def demote_headings(md: str) -> str:
    """Mintlify pinta el title como H1; si el doc usa H1 para secciones, baja todo un nivel."""
    lines, in_code, has_h1 = md.split("\n"), False, False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code and line.startswith("# "):
            has_h1 = True
            break
    if not has_h1:
        return md
    out, in_code = [], False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code and re.match(r"^#{1,5} ", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


SEP_RE = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?$")


def _is_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_empty_row(line: str) -> bool:
    return _is_row(line) and all(not c.strip() for c in line.strip().strip("|").split("|"))


def fix_tables(md: str) -> str:
    """Google exporta algunas tablas con un encabezado vacío; sube la primera fila a encabezado."""
    lines = md.split("\n")
    i = 0
    while i < len(lines) - 2:
        if _is_empty_row(lines[i]) and SEP_RE.match(lines[i + 1].strip()) and _is_row(lines[i + 2]):
            lines[i] = lines.pop(i + 2)
        i += 1
    return "\n".join(lines)


INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")
AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
BR_RE = re.compile(r"<br\s*/?>", re.I)
BR_TOKEN = "\x00BR\x00"


def _escape_text(text: str) -> str:
    text = AUTOLINK_RE.sub(r"[\1](\1)", text)          # MDX no soporta <https://...>
    text = BR_RE.sub(BR_TOKEN, text)
    text = re.sub(r"(?<!\\)([{}])", r"\\\1", text)      # { } son expresiones en MDX
    text = text.replace("<", "&lt;")                    # < suelto rompe el parser de MDX
    return text.replace(BR_TOKEN, "<br />")


def escape_mdx(md: str) -> str:
    out, in_code = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        parts, last = [], 0
        for m in INLINE_CODE_RE.finditer(line):
            parts += [_escape_text(line[last:m.start()]), m.group(0)]
            last = m.end()
        parts.append(_escape_text(line[last:]))
        out.append("".join(parts))
    return "\n".join(out)


def convert(markdown: str, slug: str, images_dir: Path, fallback_title: str) -> str:
    md = markdown.replace("\r\n", "\n").replace("\r", "\n")
    md = extract_images(md, slug, images_dir)
    title, md = split_title(md, fallback_title)
    md = demote_headings(md)
    md = fix_tables(md)
    md = escape_mdx(md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    return f"---\ntitle: {json.dumps(title, ensure_ascii=False)}\n---\n\n{md}"


# ----------------------------------------------------------------------- docs.json

def update_nav(docs_json: Path, pages: list[str]) -> None:
    if not docs_json.exists():
        print(f"  ! No hay {docs_json.name}; agrega estas páginas a la navegación a mano: {pages}")
        return
    data = json.loads(docs_json.read_text(encoding="utf-8"))
    state = {"found": False, "changed": False}

    def walk(node):
        if isinstance(node, dict):
            if node.get("group") == NAV_GROUP and "pages" in node:
                state["found"] = True
                if node["pages"] != pages:
                    node["pages"] = pages
                    state["changed"] = True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    def first_container(node):
        """Primera lista de 'groups' o 'pages' de la navegación (sirve con tabs, anchors, idiomas…)."""
        if isinstance(node, dict):
            for key in ("groups", "pages"):
                if isinstance(node.get(key), list):
                    return node[key]
            for key, value in node.items():
                if key == "global":  # links globales del header, no es el menú
                    continue
                found = first_container(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = first_container(value)
                if found is not None:
                    return found
        return None

    nav = data.setdefault("navigation", {})
    walk(nav)
    if not state["found"]:
        container = first_container(nav)
        if container is None:
            nav["groups"] = container = []
        container.append({"group": NAV_GROUP, "pages": pages})
        state["changed"] = True
    if state["changed"]:
        docs_json.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  ✓ {docs_json.name}: grupo \"{NAV_GROUP}\" actualizado")


# ---------------------------------------------------------------------------- Main

def main() -> None:
    ap = argparse.ArgumentParser(description="Sync de guías: Google Drive → Mintlify")
    ap.add_argument("--local", help="Carpeta con .md exportados, para probar sin Drive")
    ap.add_argument("--root", default=".", help="Raíz del repo de Mintlify (default: .)")
    args = ap.parse_args()
    root = Path(args.root)

    if args.local:
        sources = sources_from_dir(args.local)
    else:
        folder_id = os.environ.get("DRIVE_FOLDER_ID") or sys.exit("Falta DRIVE_FOLDER_ID.")
        sources = sources_from_drive(folder_id)
    if not sources:
        sys.exit("No hay guías para sincronizar; no toco nada.")

    guias_dir, images_dir = root / GUIAS_DIR, root / IMAGES_DIR
    for d in (guias_dir, images_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    pages, used = [], set()
    for src in sources:
        base = slug = slugify(src.name)
        n = 2
        while slug in used:
            slug, n = f"{base}-{n}", n + 1
        used.add(slug)
        mdx = convert(src.markdown, slug, images_dir / slug, humanize(src.name))
        (guias_dir / f"{slug}.mdx").write_text(mdx, encoding="utf-8")
        pages.append(f"{GUIAS_DIR}/{slug}")
        print(f"  ✓ {GUIAS_DIR}/{slug}.mdx")

    update_nav(root / DOCS_JSON, pages)
    print(f"Listo: {len(pages)} guía(s).")


if __name__ == "__main__":
    main()
