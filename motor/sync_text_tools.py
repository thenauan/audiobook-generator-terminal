"""
Texto sincronizado dos audiobooks.

Gera o texto sincronizado de um audiobook (a partir do áudio + texto do livro, ou só do áudio) numa
única passada do motor de sincronização e decide onde guardá-lo:
  - "metadata": grava nos metadados do próprio arquivo de áudio (MP3: SYLT + USLT; M4B/M4A: ©lyr)
  - "library":  salva .sync.json, .lrc e .srt na pasta da biblioteca
  - "both":     as duas coisas

Usado pelo converter.py (ao final da conversão) e pelo servidor (menu de opções do audiobook):
    python sync_text_tools.py --audio livro.m4b [--ebook livro.epub] --out-dir pasta \
        --title "Título" --author "Autor" --destination both
A última linha impressa é um JSON com o resultado (o servidor lê essa linha).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DESTINATIONS = ("metadata", "library", "both")
AUDIO_EXTS_MP3 = {".mp3"}
AUDIO_EXTS_MP4 = {".m4b", ".m4a", ".mp4", ".aac"}
AUDIO_EXTS_VORBIS = {".flac", ".opus", ".ogg"}


def safe_name(text: str) -> str:
    return re.sub(r'[\\/*?:\'"<>|]', "", text or "").strip() or "texto_sincronizado"


def build_lrc(sync_data: Dict[str, Any], title: str, author: str) -> str:
    """LRC por frases, com título e autor reais no cabeçalho."""
    from ebook_audiobook_converter import export_sync_data_to_format

    body = export_sync_data_to_format(sync_data, output_format="lrc", granularity="both")
    lines = [l for l in body.split("\n") if not re.match(r"^\[(ar|ti|re):", l)]
    header = [f"[ti:{title}]", f"[ar:{author}]", "[re:Audiobook Generator]"]
    return "\n".join(header + [l for l in lines if l.strip()]) + "\n"


def sync_items(sync_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sync_data.get("segments") or sync_data.get("sentences") or sync_data.get("words") or []


def embed_in_audio(audio_path: str, sync_data: Dict[str, Any], lrc_text: str) -> bool:
    """Grava o texto sincronizado nos metadados do arquivo. Devolve False se o formato não suporta."""
    ext = Path(audio_path).suffix.lower()
    if ext in AUDIO_EXTS_MP3:
        from mutagen.id3 import ID3, ID3NoHeaderError, SYLT, USLT, Encoding

        try:
            tags = ID3(audio_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("SYLT")
        tags.delall("USLT")
        synced = [
            (str(it.get("text", "")).strip(), int(round(float(it["start"]) * 1000)))
            for it in sync_items(sync_data)
            if str(it.get("text", "")).strip()
        ]
        # SYLT: letra com tempo (format=2 -> milissegundos; type=1 -> letra)
        tags.add(SYLT(encoding=Encoding.UTF8, lang="por", format=2, type=1, desc="Audiobook", text=synced))
        # USLT: os players que não leem SYLT costumam ler o LRC daqui
        tags.add(USLT(encoding=Encoding.UTF8, lang="por", desc="Audiobook", text=lrc_text))
        tags.save(audio_path, v2_version=3)
        return True
    if ext in AUDIO_EXTS_MP4:
        from mutagen.mp4 import MP4

        audio = MP4(audio_path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["\xa9lyr"] = [lrc_text]
        audio.save()
        return True
    if ext in AUDIO_EXTS_VORBIS:
        # FLAC e Opus: comentário LYRICS (lido pela maioria dos players)
        from mutagen.flac import FLAC
        from mutagen.oggopus import OggOpus

        audio = FLAC(audio_path) if ext == ".flac" else OggOpus(audio_path)
        if audio.tags is None:
            audio.add_tags()
        audio["LYRICS"] = lrc_text
        audio.save()
        return True
    return False  # WAV e outros: sem lugar padrão para letra


def generate_sync_text(
    audio_path: Optional[str],
    ebook_path: Optional[str],
    out_dir: str,
    title: str,
    author: str,
    destination: str = "both",
    custom_chapters: Optional[List[Dict[str, Any]]] = None,
    embed_targets: Optional[List[str]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Gera o texto e o guarda conforme o destino. Devolve o resumo do que foi feito."""
    from ebook_audiobook_converter import EbookAudiobookConverter, export_sync_data_to_format

    if destination not in DESTINATIONS:
        destination = "both"
    if not audio_path or not Path(audio_path).is_file():
        raise ValueError("Arquivo de áudio do audiobook não encontrado.")

    converter = EbookAudiobookConverter()
    sync_data = converter.convert(
        ebook_path=ebook_path or None,
        audio_filename=Path(audio_path).name,
        audio_path=audio_path,
        granularity="both",
        output_format="json",
        ai_filter=True,
        include_metadata=True,
        custom_chapters=custom_chapters,
        log_callback=log,
    )
    return save_sync_text(sync_data, audio_path, out_dir, title, author, destination, embed_targets, log)


def save_sync_text(
    sync_data: Dict[str, Any],
    audio_path: Optional[str],
    out_dir: str,
    title: str,
    author: str,
    destination: str = "both",
    embed_targets: Optional[List[str]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Grava um texto já sincronizado (segments com start/end em segundos) conforme o destino."""
    from ebook_audiobook_converter import export_sync_data_to_format
    if destination not in DESTINATIONS:
        destination = "both"
    lrc_text = build_lrc(sync_data, title, author)

    # 1) Metadados do áudio
    embedded: List[str] = []
    not_embedded: List[str] = []
    if destination in ("metadata", "both"):
        for target in (embed_targets if embed_targets is not None else ([audio_path] if audio_path else [])):
            try:
                (embedded if embed_in_audio(target, sync_data, lrc_text) else not_embedded).append(target)
            except Exception as e:  # o arquivo pode estar aberto em outro programa
                not_embedded.append(target)
                if log:
                    log(f"Aviso: não foi possível gravar o texto em '{Path(target).name}': {e}")

    # 2) Pasta da biblioteca (também usada quando não deu para gravar nos metadados)
    save_files = destination in ("library", "both") or (destination == "metadata" and not embedded)
    files: List[str] = []
    if save_files:
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, safe_name(title))
        outputs = {
            f"{base}.sync.json": json.dumps(sync_data, ensure_ascii=False, indent=2),
            f"{base}.lrc": lrc_text,
            f"{base}.srt": export_sync_data_to_format(sync_data, output_format="srt", granularity="both"),
        }
        for path, content in outputs.items():
            Path(path).write_text(content, encoding="utf-8")
            files.append(path)

    return {
        "success": True,
        "destination": destination,
        "embedded": embedded,
        "notEmbedded": not_embedded,
        "savedToLibrary": save_files,
        "files": files,
        "content": lrc_text,
        "format": "lrc",
        "wordCount": len(re.findall(r"\w+", " ".join(str(i.get("text", "")) for i in sync_items(sync_data)))),
        "fallbackToLibrary": destination == "metadata" and not embedded,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Gera o texto sincronizado de um audiobook.")
    parser.add_argument("--audio", required=True, help="Arquivo de áudio do audiobook (.mp3, .m4b...)")
    parser.add_argument("--ebook", default=None, help="Texto do livro (.epub, .pdf, .mobi, .txt). Sem ele, transcreve o áudio.")
    parser.add_argument("--out-dir", required=True, help="Pasta da biblioteca")
    parser.add_argument("--title", default="Audiobook")
    parser.add_argument("--author", default="")
    parser.add_argument("--destination", choices=DESTINATIONS, default="both")
    args = parser.parse_args()

    def log(msg: str) -> None:
        print(json.dumps({"type": "log", "message": msg}, ensure_ascii=False), flush=True)

    try:
        result = generate_sync_text(
            audio_path=args.audio,
            ebook_path=args.ebook,
            out_dir=args.out_dir,
            title=args.title,
            author=args.author,
            destination=args.destination,
            log=log,
        )
        print(json.dumps({"type": "result", **result}, ensure_ascii=False), flush=True)
    except Exception as e:
        print(json.dumps({"type": "result", "success": False, "error": str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
