"""Safe, plain-text book extraction for the FluxConvert reader."""
import os
import posixpath
import subprocess
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

MAX_TEXT = 4_000_000
SUPPORTED_BOOKS = {"txt", "md", "text", "pdf", "doc", "docx", "odt", "rtf", "epub", "fb2", "html", "htm"}

class BookHTMLParser(HTMLParser):
    BREAK_TAGS = {"p", "div", "section", "article", "blockquote", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr"}
    HIDE_TAGS = {"script", "style", "head", "svg", "nav"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.HIDE_TAGS:
            self.hidden += 1
        elif not self.hidden and tag in self.BREAK_TAGS:
            self.out.append("\n\n" if tag.startswith("h") or tag in {"p", "section", "article"} else "\n")
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.HIDE_TAGS:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in self.BREAK_TAGS:
            self.out.append("\n")
    def handle_data(self, data):
        if not self.hidden:
            self.out.append(data)
    def extract(self, html):
        self.feed(html)
        self.close()
        return "".join(self.out)

def decode(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1251")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")

def plain_html(data):
    return BookHTMLParser().extract(decode(data))

def extract_epub(path):
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > 2500 or sum(x.file_size for x in infos) > 100 * 1024 * 1024:
            raise ValueError("Слишком большой или повреждённый EPUB.")
        names = set(z.namelist())
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = next((x.attrib["full-path"] for x in container.iter()
                         if x.tag.rsplit("}", 1)[-1] == "rootfile"), None)
        if not rootfile or rootfile not in names:
            raise ValueError("Не найдена структура EPUB.")
        opf = ET.fromstring(z.read(rootfile))
        manifest = {x.attrib.get("id"): x.attrib for x in opf.iter()
                    if x.tag.rsplit("}", 1)[-1] == "item"}
        spine = [x.attrib.get("idref") for x in opf.iter()
                 if x.tag.rsplit("}", 1)[-1] == "itemref"]
        base = posixpath.dirname(rootfile)
        pieces = []
        total = 0
        for item_id in spine:
            item = manifest.get(item_id)
            if not item or "html" not in item.get("media-type", ""):
                continue
            href = item.get("href", "").split("#")[0].split("?")[0]
            relative = posixpath.normpath(posixpath.join(base, href))
            if relative.startswith("../") or relative.startswith("/") or relative not in names:
                continue
            if z.getinfo(relative).file_size > 8 * 1024 * 1024:
                continue
            part = plain_html(z.read(relative))
            total += len(part)
            if total > MAX_TEXT:
                raise ValueError("Текст книги слишком большой.")
            pieces.append(part)
        return "\n\n".join(pieces)

def extract_fb2(path):
    root = ET.parse(path).getroot()
    paras = []
    total = 0
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] in {"p", "v", "subtitle"}:
            line = "".join(node.itertext()).strip()
            if line:
                total += len(line)
                if total > MAX_TEXT:
                    raise ValueError("Текст книги слишком большой.")
                paras.append(line)
    return "\n\n".join(paras)

def extract_pdf(path):
    import fitz
    pieces = []
    total = 0
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("PDF защищён паролем.")
        if doc.page_count > 1800:
            raise ValueError("PDF содержит слишком много страниц.")
        for page in doc:
            block = page.get_text("text", sort=True)
            total += len(block)
            if total > MAX_TEXT:
                raise ValueError("Текст книги слишком большой.")
            pieces.append(block)
    return "\n\n".join(pieces)

def office_to_text(path, folder):
    out = Path(folder) / "output"
    out.mkdir()
    profile = (Path(folder) / "lo-profile").as_uri()
    args = ["libreoffice", "-env:UserInstallation=" + profile,
            "--headless", "--convert-to", "txt:Text (encoded):UTF8",
            "--outdir", str(out), str(path)]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=120, check=False)
    target = out / (path.stem + ".txt")
    if result.returncode or not target.is_file():
        raise ValueError("Не удалось прочитать этот документ.")
    if target.stat().st_size > MAX_TEXT * 4:
        raise ValueError("Текст книги слишком большой.")
    return decode(target.read_bytes())

def extract_book(path, suffix, folder):
    if suffix not in SUPPORTED_BOOKS:
        raise ValueError("Формат книги не поддерживается.")
    if suffix in {"txt", "md", "text"}:
        result = decode(path.read_bytes())
    elif suffix in {"html", "htm"}:
        result = plain_html(path.read_bytes())
    elif suffix == "pdf":
        result = extract_pdf(path)
    elif suffix == "epub":
        result = extract_epub(path)
    elif suffix == "fb2":
        result = extract_fb2(path)
    else:
        result = office_to_text(path, folder)
    import re
    result = result.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    result = re.sub(r"[ \t]+\n", "\n", result)
    result = re.sub(r"\n{4,}", "\n\n\n", result).strip()
    if not result:
        raise ValueError("Текст не найден. Сканированные PDF без текстового слоя пока не поддерживаются.")
    if len(result) > MAX_TEXT:
        raise ValueError("Книга слишком большая для режима чтения.")
    return result
