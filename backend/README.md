# FluxConvert — backend and Render deployment

This branch contains the static FluxConvert website at `static/index.html`, plus the Python FastAPI backend under `backend/`.

## Deploy on Render

1. In Render, open **New → Blueprint** and choose `nurislombek006-pixel/site`.
2. Choose the `fluxconvert` branch and the `render.yaml` blueprint.
3. Review the plan and confirm deployment.
4. When deployment finishes, open the new `onrender.com` service URL.
5. Check `/api/health` and try a small DOCX→PDF or PPTX→PDF conversion.

The site and backend are served together at the Render URL. GitHub Pages is **static** and cannot call `/api/convert` itself. Use the Render URL to convert Office, PDFs, audio, and video.

**Free plan warning:** Render free web services have tight memory/CPU limits and may sleep when idle. LibreOffice + 53 MB presentations might fail due to insufficient memory. Consider a paid instance with at least 2 GB RAM after user approval.

**Security warning:** This is a prototype, not a production-grade public file-conversion service. Before broad public launch add rate limits, malware scanning, strong sandboxing, queues, and monitoring. Temporary files are deleted on completion.

Tests, other instructions and full original source are in the conversation ZIP archive.
