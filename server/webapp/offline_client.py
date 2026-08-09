from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Sequence


def _safe_filename(value: str) -> str:
    import re
    value = re.sub(r'[\\/:*?"<>|]+', "-", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:160] or "mangabridge-offline"


def offline_bundle_html(manifest: Dict[str, Any]) -> str:
    manifest_json = json.dumps(manifest, ensure_ascii=False).replace("</", "<\\/")
    template = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>MangaBridge Offline Reader</title>
<style>
:root{--bg:#0d0b11;--panel:#18151f;--line:#3c3544;--ink:#f4eef6;--muted:#b6abbc;--red:#f45b69;--cyan:#65d9e8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
button,select,a{font:inherit}button,.button{border:1px solid var(--line);background:#292330;color:var(--ink);padding:.65rem .85rem;border-radius:5px;font-weight:750;text-decoration:none;cursor:pointer}button:hover,.button:hover{border-color:var(--cyan)}
.shell{min-height:100vh;display:grid;grid-template-columns:280px 1fr}aside{background:var(--panel);border-right:1px solid var(--line);padding:1rem;overflow:auto}.brand{font-weight:950;letter-spacing:.03em;text-transform:uppercase}.brand span{color:var(--red)}h1{font-size:1.35rem;margin:.5rem 0 1rem}.chapter-list{display:grid;gap:.35rem}.chapter-list button{text-align:left;width:100%}.chapter-list button.active{background:var(--red);border-color:var(--red)}main{min-width:0;display:grid;grid-template-rows:auto 1fr}header{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;padding:.7rem;background:#121017;border-bottom:1px solid var(--line)}header strong{margin-right:auto}select{background:#221e28;color:var(--ink);border:1px solid var(--line);padding:.6rem;max-width:260px}.viewer{min-height:0;position:relative}iframe{display:block;width:100%;height:calc(100vh - 66px);border:0;background:#5f5f5f}.fallback{position:absolute;inset:auto 1rem 1rem 1rem;background:rgba(13,11,17,.92);border:1px solid var(--line);padding:.7rem;color:var(--muted)}.fallback a{color:var(--cyan)}small{color:var(--muted)}
@media(max-width:760px){.shell{grid-template-columns:1fr}aside{display:none}header strong{width:100%}iframe{height:calc(100vh - 118px)}}
</style>
</head>
<body>
<div class="shell">
<aside><div class="brand"><span>漫画</span> MangaBridge</div><small>Portable offline library</small><h1 id="side-title"></h1><div class="chapter-list" id="chapter-list"></div></aside>
<main><header><strong id="title"></strong><button id="prev">Previous</button><select id="chapter-select"></select><button id="next">Next</button><a class="button" id="open-file" href="#">Open PDF</a></header><div class="viewer"><iframe id="viewer" title="Offline manga PDF"></iframe><div class="fallback">If your browser does not embed local PDFs, use <a id="fallback-link" href="#">Open PDF</a>. The chapter files are also available in the <code>chapters</code> folder.</div></div></main>
</div>
<script type="application/json" id="manifest">__MANIFEST__</script>
<script>
(()=>{
const data=JSON.parse(document.getElementById('manifest').textContent);const chapters=data.chapters||[];const key='mangabridge-offline:'+String(data.series.id||data.series.title||'library');
const title=document.getElementById('title'),side=document.getElementById('side-title'),select=document.getElementById('chapter-select'),list=document.getElementById('chapter-list'),frame=document.getElementById('viewer'),open=document.getElementById('open-file'),fallback=document.getElementById('fallback-link');
side.textContent=data.series.title;chapters.forEach((ch,i)=>{const o=document.createElement('option');o.value=String(i);o.textContent='Chapter '+ch.number;select.appendChild(o);const b=document.createElement('button');b.textContent='Chapter '+ch.number;b.dataset.index=String(i);b.onclick=()=>show(i);list.appendChild(b);});
function show(raw){if(!chapters.length)return;const i=Math.max(0,Math.min(chapters.length-1,Number(raw)||0)),ch=chapters[i];select.value=String(i);title.textContent=data.series.title+' · Chapter '+ch.number;frame.src=ch.file+'#view=FitH';open.href=ch.file;fallback.href=ch.file;document.querySelectorAll('#chapter-list button').forEach((b,n)=>b.classList.toggle('active',n===i));document.getElementById('prev').disabled=i===0;document.getElementById('next').disabled=i===chapters.length-1;try{localStorage.setItem(key,String(i))}catch(e){}}
select.onchange=()=>show(select.value);document.getElementById('prev').onclick=()=>show(Number(select.value)-1);document.getElementById('next').onclick=()=>show(Number(select.value)+1);document.addEventListener('keydown',e=>{if(e.key==='ArrowLeft')show(Number(select.value)-1);if(e.key==='ArrowRight')show(Number(select.value)+1)});
let start=0;try{start=Number(localStorage.getItem(key)||0)}catch(e){}show(start);
})();
</script>
</body></html>'''
    return template.replace("__MANIFEST__", manifest_json)


def build_bundle(
    *,
    title: str,
    series_id: str,
    created_at: str,
    chapters: Sequence[Dict[str, Any]],
):
    manifest = {
        "format": "mangabridge-offline-library-v1",
        "created_at": created_at,
        "series": {"id": series_id, "title": title},
        "chapters": [
            {
                "number": item["number"],
                "file": f"chapters/chapter-{item['number']:g}.pdf",
                "size": item["size"],
            }
            for item in chapters
        ],
    }
    archive = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024, mode="w+b")
    # Chapter PDFs are already compressed image containers. Storing them avoids
    # burning CPU trying to recompress hundreds of megabytes for client export.
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
        bundle.writestr("index.html", offline_bundle_html(manifest))
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        bundle.writestr(
            "README.txt",
            "MangaBridge portable offline library\n\n"
            "Unzip this folder and open index.html in a modern browser. "
            "The original chapter PDFs are in the chapters folder and can also be opened directly.\n",
        )
        for item in chapters:
            bundle.write(Path(item["path"]), arcname=f"chapters/chapter-{item['number']:g}.pdf")
    archive.seek(0)
    return archive, _safe_filename(f"{title} - MangaBridge offline") + ".zip"
