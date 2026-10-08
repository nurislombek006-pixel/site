from __future__ import annotations
import os, shutil, subprocess, tempfile, zipfile
from pathlib import Path
import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles

HERE=Path(__file__).parent
MAX_UPLOAD=int(os.getenv("MAX_UPLOAD_MB","80"))*1024*1024
MAX_OUTPUT=int(os.getenv("MAX_OUTPUT_MB","160"))*1024*1024
MAX_PDF_PAGES=int(os.getenv("MAX_PDF_PAGES","60"))
TIMEOUT=int(os.getenv("CONVERT_TIMEOUT","120"))
IMAGES=("jpg","jpeg","png","webp","bmp","gif","tif","tiff","ico")
IMAGE_TARGETS=("png","jpg","webp","bmp","gif","tiff","pdf")
DOCS={
"doc":("docx","odt","rtf","pdf","txt"),
"docx":("doc","odt","rtf","pdf","txt"),
"odt":("docx","doc","rtf","pdf","txt"),
"rtf":("docx","odt","pdf","txt"),
"txt":("docx","odt","rtf","pdf")}
SHEETS={"xls":("xlsx","ods","csv","pdf"),"xlsx":("xls","ods","csv","pdf"),
"ods":("xlsx","xls","csv","pdf"),"csv":("xlsx","ods","pdf")}
PRESENTATIONS={"ppt":("pptx","odp","pdf"),"pptx":("ppt","odp","pdf"),"odp":("pptx","ppt","pdf")}
AUDIOS=("mp3","wav","ogg","flac","m4a","aac","wma")
AUDIO_TARGETS=("mp3","wav","ogg","flac","m4a","aac")
VIDEOS=("mp4","mov","mkv","webm","avi")
VIDEO_TARGETS=("mp4","webm","mkv","mp3","wav","m4a")
MATRIX={}
for e in IMAGES: MATRIX[e]=[x for x in IMAGE_TARGETS if x!=e and not (e=="jpeg" and x=="jpg")]
for group in (DOCS,SHEETS,PRESENTATIONS):
    for e,targets in group.items(): MATRIX[e]=list(targets)
for e in AUDIOS: MATRIX[e]=[x for x in AUDIO_TARGETS if x!=e]
for e in VIDEOS: MATRIX[e]=[x for x in VIDEO_TARGETS if x!=e]
MATRIX["pdf"]=["docx","txt","png","jpg"]
CATEGORIES=[
{"id":"all","name":"Все форматы","extensions":list(MATRIX)},
{"id":"images","name":"Изображения","extensions":list(IMAGES)},
{"id":"documents","name":"Документы","extensions":list(DOCS)+["pdf"]},
{"id":"presentations","name":"Презентации","extensions":list(PRESENTATIONS)},
{"id":"spreadsheets","name":"Таблицы","extensions":list(SHEETS)},
{"id":"audio","name":"Аудио","extensions":list(AUDIOS)},
{"id":"video","name":"Видео","extensions":list(VIDEOS)}]
app=FastAPI(title="FluxConvert",docs_url=None,redoc_url=None)

def run_command(args):
    try:
        proc=subprocess.run(args,timeout=TIMEOUT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    except FileNotFoundError as exc:
        raise HTTPException(503,"На сервере отсутствует необходимая программа.") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(408,"Время обработки истекло. Уменьшите файл или повторите попытку.") from exc
    if proc.returncode:
        raise HTTPException(422,"Не удалось конвертировать файл. Возможно, он повреждён или формат не поддерживается.")

def image_convert(source, dest, fmt):
    try:
        Image.MAX_IMAGE_PIXELS=45_000_000
        with Image.open(source) as raw:
            img=ImageOps.exif_transpose(raw)
            img.seek(0)
            if fmt in ("jpg","pdf"):
                if "A" in img.getbands() or "transparency" in img.info:
                    rgba=img.convert("RGBA")
                    background=Image.new("RGB",rgba.size,"white")
                    background.paste(rgba,mask=rgba.getchannel("A"))
                    img=background
                else: img=img.convert("RGB")
            elif fmt=="gif": img=img.convert("P",palette=Image.Palette.ADAPTIVE)
            elif fmt=="bmp": img=img.convert("RGB")
            elif fmt in ("png","webp","tiff"):
                img=img.convert("RGBA" if ("A" in img.getbands() or "transparency" in img.info) else "RGB")
            if fmt=="jpg": img.save(dest,format="JPEG",quality=90,optimize=True)
            elif fmt=="pdf": img.save(dest,format="PDF",resolution=150)
            else: img.save(dest,format={"tiff":"TIFF"}.get(fmt,fmt.upper()))
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise HTTPException(422,"Не удалось открыть изображение.") from exc

def office_convert(source, outdir, target, workspace):
    profile=workspace/"lo-profile"
    profile.mkdir()
    run_command(["libreoffice","-env:UserInstallation="+profile.resolve().as_uri(),
        "--headless","--convert-to",target,"--outdir",str(outdir),str(source)])
    result=outdir/("source."+target)
    if not result.is_file(): raise HTTPException(422,"LibreOffice не смог преобразовать этот файл.")
    return result

def pdf_convert(source, outdir, target):
    try:
        with fitz.open(source) as doc:
            if doc.is_encrypted: raise HTTPException(422,"PDF защищён паролем.")
            if len(doc)>MAX_PDF_PAGES: raise HTTPException(413,"Слишком много страниц PDF.")
            if target=="txt":
                p=outdir/"source.txt"
                p.write_text("\n\n".join(page.get_text() for page in doc),encoding="utf-8")
                return p
            if target in ("jpg","png"):
                p=outdir/"source.zip"
                with zipfile.ZipFile(p,"w",compression=zipfile.ZIP_DEFLATED) as z:
                    for i,page in enumerate(doc,start=1):
                        pix=page.get_pixmap(matrix=fitz.Matrix(1.7,1.7),alpha=False)
                        z.writestr(f"page_{i:03d}.{target}",pix.tobytes("jpeg" if target=="jpg" else "png"))
                return p
    except HTTPException: raise
    except (RuntimeError,ValueError) as exc:
        raise HTTPException(422,"Не удалось прочитать PDF.") from exc
    if target=="docx":
        try:
            from pdf2docx import Converter
            p=outdir/"source.docx"
            c=Converter(str(source))
            try: c.convert(str(p))
            finally: c.close()
            return p
        except Exception as exc:
            raise HTTPException(422,"Не удалось восстановить Word из PDF. Возможно, нужен OCR.") from exc
    raise HTTPException(415,"Недопустимый выходной формат для PDF.")

def media_convert(source, dest, target):
    cmd=["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-y","-threads","2","-i",str(source)]
    codecs={
        "mp3":["-c:a","libmp3lame","-q:a","3"],
        "wav":["-c:a","pcm_s16le"],
        "ogg":["-c:a","libvorbis","-q:a","5"],
        "flac":["-c:a","flac"],
        "m4a":["-c:a","aac","-b:a","192k"],
        "aac":["-c:a","aac","-b:a","192k"]}
    if target in codecs: cmd+=["-vn","-map","0:a:0",*codecs[target]]
    elif target=="webm": cmd+=["-map","0:v:0","-map","0:a?","-c:v","libvpx-vp9","-b:v","1200k","-deadline","realtime","-cpu-used","6","-c:a","libopus"]
    else: cmd+=["-map","0:v:0","-map","0:a?","-c:v","libx264","-preset","veryfast","-crf","24","-pix_fmt","yuv420p","-c:a","aac","-b:a","160k"]
    run_command(cmd+[str(dest)])

def convert_file(source, src, dst, outdir, workdir):
    result=outdir/("source."+dst)
    if src in IMAGES: image_convert(source,result,dst)
    elif src=="pdf": result=pdf_convert(source,outdir,dst)
    elif src in (*DOCS,*SHEETS,*PRESENTATIONS): result=office_convert(source,outdir,dst,workdir)
    elif src in (*AUDIOS,*VIDEOS): media_convert(source,result,dst)
    else: raise HTTPException(415,"Формат не поддерживается.")
    return result

@app.get("/api/health")
def health(): return {"ok":True}

@app.get("/api/formats")
def formats(): return {"matrix":MATRIX,"categories":CATEGORIES,"max_upload_mb":MAX_UPLOAD//1048576}

@app.post("/api/convert")
async def convert(file: UploadFile=File(...), target: str=Form(...)):
    original=Path(file.filename or "file").name
    if "." not in original: raise HTTPException(400,"У файла отсутствует расширение.")
    src=original.rsplit(".",1)[-1].lower()
    dst=target.strip().lower().lstrip(".")
    if src not in MATRIX: raise HTTPException(415,"Исходный формат не поддерживается.")
    if dst not in MATRIX[src]: raise HTTPException(400,"Это преобразование не поддерживается.")
    workdir=Path(tempfile.mkdtemp(prefix="flux-"))
    try:
        source=workdir/("source."+src)
        outdir=workdir/"out"
        outdir.mkdir()
        amount=0
        with source.open("wb") as stream:
            while chunk:=await file.read(1048576):
                amount+=len(chunk)
                if amount>MAX_UPLOAD: raise HTTPException(413,"Максимальный размер файла: "+str(MAX_UPLOAD//1048576)+" МБ.")
                stream.write(chunk)
        await file.close()
        if not amount: raise HTTPException(400,"Файл пустой.")
        result=await run_in_threadpool(convert_file,source,src,dst,outdir,workdir)
        if not result.is_file() or result.stat().st_size==0: raise HTTPException(422,"Файл результата не создан.")
        if result.stat().st_size>MAX_OUTPUT: raise HTTPException(413,"Результат слишком большой.")
        return FileResponse(
            path=result,
            filename=(Path(original).stem[:90] or "converted")+result.suffix,
            media_type="application/octet-stream",
            background=BackgroundTask(shutil.rmtree,workdir,ignore_errors=True),
            headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
    except Exception:
        shutil.rmtree(workdir,ignore_errors=True)
        raise

app.mount("/", StaticFiles(directory=HERE/"static",html=True),name="site")
