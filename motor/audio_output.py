"""
Saída de áudio dos audiobooks: formatos, qualidade, normalização, montagem com capítulos e tags.

Fluxo de qualidade:
  1. A voz vem do Edge TTS em MP3 24 kHz a 96 kbps (o dobro do padrão da biblioteca edge-tts).
  2. Cada capítulo é normalizado (EBU R128) e guardado SEM PERDA (FLAC) no cache.
  3. O arquivo final é codificado UMA vez, no formato e na qualidade escolhidos.
Taxas maiores ou formatos sem perda preservam a voz sem novas perdas, mas não criam detalhes que a
origem (voz sintetizada a 24 kHz) não tem.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import types
from typing import Any, Dict, List, Optional

# ----- Voz em maior qualidade -----
FORMATO_PADRAO_EDGE = "audio-24khz-48kbitrate-mono-mp3"
FORMATO_ALTA_EDGE = "audio-24khz-96kbitrate-mono-mp3"
_communicate_hq = None


def obter_communicate():
    """Classe Communicate do edge-tts pedindo 96 kbps. Se não for possível, devolve a padrão."""
    global _communicate_hq
    import edge_tts
    if _communicate_hq is not None:
        return _communicate_hq
    try:
        import edge_tts.communicate as cm
        src = open(cm.__file__, encoding="utf-8").read()
        if FORMATO_PADRAO_EDGE not in src:
            _communicate_hq = edge_tts.Communicate
            return _communicate_hq
        mod = types.ModuleType("edge_tts._communicate_hq")
        mod.__package__ = "edge_tts"
        exec(compile(src.replace(FORMATO_PADRAO_EDGE, FORMATO_ALTA_EDGE), cm.__file__, "exec"), mod.__dict__)
        mod.MP3_BITRATE_BPS = 96_000  # usado pelo edge-tts para calcular tempos de palavras
        _communicate_hq = mod.Communicate
    except Exception:
        _communicate_hq = edge_tts.Communicate
    return _communicate_hq


# ----- Formatos de saída -----
# value (o mesmo usado no app) -> extensão, codificador, taxa por qualidade, taxa de amostragem
FORMATOS: Dict[str, Dict[str, Any]] = {
    "single":   {"ext": ".mp3",  "codec": "libmp3lame", "bitrate": {"voice": "96k", "standard": "128k", "high": "192k", "max": "320k"}, "ar": 44100},
    "multiple": {"ext": ".mp3",  "codec": "libmp3lame", "bitrate": {"voice": "96k", "standard": "128k", "high": "192k", "max": "320k"}, "ar": 44100},
    "m4b":      {"ext": ".m4b",  "codec": "aac",        "bitrate": {"voice": "64k", "standard": "96k",  "high": "128k", "max": "256k"}, "ar": 44100, "muxer": "mp4"},
    "opus":     {"ext": ".opus", "codec": "libopus",    "bitrate": {"voice": "40k", "standard": "48k",  "high": "64k",  "max": "128k"}, "ar": 48000},
    "flac":     {"ext": ".flac", "codec": "flac",       "bitrate": None, "ar": None},
    "wav":      {"ext": ".wav",  "codec": "pcm_s16le",  "bitrate": None, "ar": None},
}
# "voice": do tamanho da voz de origem (MP3 24 kHz a 96 kbps), sem reamostrar — metade do tamanho de "high",
# mesmo som. As outras sobem a taxa (44,1 kHz e mais kbps), o que não acrescenta detalhe à voz sintetizada.
QUALIDADES = ("voice", "standard", "high", "max")
EXTENSOES_AUDIO = {".mp3", ".m4b", ".m4a", ".opus", ".ogg", ".flac", ".wav"}


def formato(fmt: Optional[str]) -> str:
    return fmt if fmt in FORMATOS else "single"


def qualidade(q: Optional[str]) -> str:
    return q if q in QUALIDADES else "high"


def argumentos_codec(fmt: str, q: str) -> List[str]:
    spec = FORMATOS[formato(fmt)]
    args = ["-c:a", spec["codec"], "-ac", "1"]
    if spec["bitrate"]:
        args += ["-b:a", spec["bitrate"][qualidade(q)]]
    # Perfil "voice": MP3 e AAC na taxa de amostragem da voz (24 kHz); o Opus trabalha sempre a 48 kHz
    ar = 24000 if qualidade(q) == "voice" and spec["codec"] in ("libmp3lame", "aac") else spec["ar"]
    if ar:
        args += ["-ar", str(ar)]
    if spec["codec"] == "libopus":
        args += ["-application", "voip" if qualidade(q) == "standard" else "audio"]
    if spec["codec"] == "flac":
        args += ["-compression_level", "8"]
    return args


def ffmpeg_disponivel() -> bool:
    return shutil.which("ffmpeg") is not None


def _rodar(cmd: List[str]) -> subprocess.CompletedProcess:
    # Sem janela no Windows (o FFmpeg é um programa de console)
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace", creationflags=flags)
    except OSError as e:  # programa não instalado: falha como um código de saída, para quem chamou decidir
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _rodar_com_progresso(cmd: List[str], total_ms: int, ao_progresso=None) -> subprocess.CompletedProcess:
    """Roda o FFmpeg informando quanto do áudio já foi gerado (fração de 0 a 1), lendo o -progress dele."""
    if not ao_progresso or total_ms <= 0 or not ffmpeg_disponivel():
        return _rodar(cmd)
    flags = 0x08000000 if os.name == "nt" else 0
    cmd = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=flags)
    import threading
    erros: List[str] = []
    leitor = threading.Thread(target=lambda: erros.append(proc.stderr.read()), daemon=True)
    leitor.start()
    ultimo = -1.0
    for linha in proc.stdout:
        if linha.startswith("out_time_us=") or linha.startswith("out_time_ms="):
            try:
                fracao = min(1.0, int(linha.split("=", 1)[1]) / 1000 / total_ms)
            except ValueError:
                continue
            if fracao - ultimo >= 0.01:
                ultimo = fracao
                try:
                    ao_progresso(fracao)
                except Exception:
                    pass
    proc.wait()
    leitor.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, "", "".join(erros))


# ----- Capítulos -----
def normalizar_capitulo(entrada_mp3: str, saida_flac: str) -> bool:
    """Pausa de 1,5 s no início + normalização EBU R128 (-18 LUFS), guardado sem perda (FLAC 24 kHz)."""
    if not ffmpeg_disponivel():
        return False  # sem FFmpeg: o capítulo fica com a voz como veio
    cmd = ["ffmpeg", "-y", "-i", entrada_mp3,
           "-af", "adelay=1500|1500,loudnorm=I=-18:TP=-1.5:LRA=10",
           "-c:a", "flac", "-ar", "24000", "-ac", "1", saida_flac]
    res = _rodar(cmd)
    if res.returncode == 0 and os.path.exists(saida_flac) and os.path.getsize(saida_flac) > 1000:
        return True
    # Sem o filtro (ex.: áudio curto demais para o loudnorm): só converte sem perda
    res = _rodar(["ffmpeg", "-y", "-i", entrada_mp3, "-af", "adelay=1500|1500", "-c:a", "flac", "-ac", "1", saida_flac])
    return res.returncode == 0 and os.path.exists(saida_flac) and os.path.getsize(saida_flac) > 1000


def duracao_ms(caminho: str) -> int:
    try:
        import mutagen
        info = mutagen.File(caminho)
        if info is not None and info.info and info.info.length:
            return int(info.info.length * 1000)
    except Exception:
        pass
    res = _rodar(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", caminho])
    try:
        return int(float(res.stdout.strip()) * 1000)
    except Exception:
        return 0


def capa_bytes(cover_base64: Optional[str]):
    """(bytes, mime) da capa em base64/data URL, ou (None, None)."""
    if not cover_base64:
        return None, None
    try:
        dados = str(cover_base64).strip()
        mime = "image/jpeg"
        if dados.startswith("data:"):
            cabecalho, dados = dados.split(",", 1)
            for m in ("image/png", "image/webp", "image/svg+xml"):
                if m in cabecalho:
                    mime = m
        return base64.b64decode(dados), mime
    except Exception:
        return None, None


def _ffmetadata(caminho: str, titulo: str, autor: str, genero: Optional[str], narrador: Optional[str], capitulos: List[Dict[str, Any]]):
    def esc(v: Any) -> str:
        return str(v).replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        f.write(f"title={esc(titulo)}\nartist={esc(autor)}\nalbum={esc(titulo)}\n")
        if genero:
            f.write(f"genre={esc(genero)}\n")
        if narrador:
            f.write(f"composer={esc(narrador)}\n")
        f.write("\n")
        for ch in capitulos:
            f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={ch['start_time']}\nEND={ch['end_time']}\ntitle={esc(ch['title'])}\n\n")


def aplicar_ano(caminho: str, ano: Optional[str]) -> None:
    """Ano de publicação nas tags do arquivo final (MP3: TDRC; M4B/M4A: ©day; FLAC/Opus: DATE)."""
    ano = str(ano or "").strip()
    if not ano:
        return
    ext = os.path.splitext(caminho)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TDRC, ID3NoHeaderError
            try:
                tags = ID3(caminho)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TDRC(encoding=3, text=ano))
            tags.save(caminho, v2_version=3)
        elif ext in (".m4b", ".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            audio = MP4(caminho)
            if audio.tags is None:
                audio.add_tags()
            audio.tags["©day"] = [ano]
            audio.save()
        elif ext in (".flac", ".opus", ".ogg"):
            from mutagen.flac import FLAC
            from mutagen.oggopus import OggOpus
            audio = FLAC(caminho) if ext == ".flac" else OggOpus(caminho)
            audio["DATE"] = ano
            audio.save()
    except Exception:
        pass  # o ano é um detalhe: nunca falha a conversão por causa dele


def _tempo_capitulo(ms: int) -> str:
    h, resto = divmod(int(ms), 3_600_000)
    m, resto = divmod(resto, 60_000)
    s, ms = divmod(resto, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def aplicar_tags_vorbis(caminho: str, titulo: str, autor: str, genero: Optional[str], narrador: Optional[str],
                        cover_base64: Optional[str], capitulos: Optional[List[Dict[str, Any]]] = None, faixa: Optional[int] = None,
                        album: Optional[str] = None) -> None:
    """Tags, capítulos (CHAPTERxxx) e capa (METADATA_BLOCK_PICTURE) em FLAC e Opus."""
    from mutagen.flac import FLAC, Picture
    from mutagen.oggopus import OggOpus

    ext = os.path.splitext(caminho)[1].lower()
    audio = FLAC(caminho) if ext == ".flac" else OggOpus(caminho)
    if audio.tags is None:
        audio.add_tags()
    audio["TITLE"] = titulo
    audio["ALBUM"] = album or titulo
    audio["ARTIST"] = autor
    if genero:
        audio["GENRE"] = str(genero)
    if narrador:
        audio["PERFORMER"] = str(narrador)
    if faixa:
        audio["TRACKNUMBER"] = str(faixa)
    for i, ch in enumerate(capitulos or [], start=1):
        audio[f"CHAPTER{i:03d}"] = _tempo_capitulo(ch["start_time"])
        audio[f"CHAPTER{i:03d}NAME"] = str(ch["title"])

    dados, mime = capa_bytes(cover_base64)
    if dados and mime != "image/svg+xml":
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = "Cover"
        pic.data = dados
        if ext == ".flac":
            audio.clear_pictures()
            audio.add_picture(pic)
        else:
            audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


def gerar_arquivo_unico(saida: str, fmt: str, q: str, capitulos: List[Dict[str, Any]], titulo: str, autor: str,
                        genero: Optional[str], narrador: Optional[str], cover_base64: Optional[str], cache_dir: str,
                        ao_progresso=None) -> None:
    """Junta os capítulos (FLAC do cache) num arquivo só, no formato escolhido, com capítulos e capa."""
    fmt = formato(fmt)
    if not ffmpeg_disponivel():
        _juntar_mp3_sem_ffmpeg(saida, fmt, capitulos, ao_progresso)
        return
    meta_path = os.path.join(cache_dir, "metadata.txt")
    _ffmetadata(meta_path, titulo, autor, genero, narrador, capitulos)
    concat_path = os.path.join(cache_dir, "concat.txt")
    with open(concat_path, "w", encoding="utf-8") as f:
        for ch in capitulos:
            # Caminho absoluto: o concat do FFmpeg resolve caminhos relativos a partir da pasta deste arquivo
            f.write("file '{}'\n".format(os.path.abspath(ch["file"]).replace("\\", "/").replace("'", "'\\''")))

    base = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_path, "-i", meta_path]
    codec = argumentos_codec(fmt, q)
    total_ms = int(capitulos[-1]["end_time"]) if capitulos else 0

    def rodar(cmd):
        return _rodar_com_progresso(cmd, total_ms, ao_progresso)

    if fmt == "m4b":
        dados, mime = capa_bytes(cover_base64)
        capa = None
        if dados and mime in ("image/jpeg", "image/png"):
            capa = os.path.join(cache_dir, "cover.jpg" if mime == "image/jpeg" else "cover.png")
            with open(capa, "wb") as cf:
                cf.write(dados)
        cmd = base + (["-i", capa, "-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"] if capa else ["-map", "0:a"])
        cmd += ["-map_metadata", "1", "-map_chapters", "1"] + codec + ["-movflags", "+faststart", "-f", "mp4", saida]
        res = rodar(cmd)
        if res.returncode != 0 and capa:  # capa incompatível: gera sem ela
            res = rodar(base + ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"] + codec + ["-f", "mp4", saida])
    elif fmt in ("opus", "flac"):
        res = rodar(base + ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "-1"] + codec + [saida])
        if res.returncode == 0:
            aplicar_tags_vorbis(saida, titulo, autor, genero, narrador, cover_base64, capitulos)
    else:
        # MP3 (capítulos e capa entram depois via ID3) e WAV (só título/autor: o formato não guarda capítulos)
        res = rodar(base + ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "-1"] + codec + [saida])

    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg falhou ao gerar {FORMATOS[fmt]['ext']}: {res.stderr[-300:]}")


def _sem_ffmpeg_so_mp3(fmt: str, arquivos: List[str]) -> None:
    if fmt not in ("single", "multiple"):
        raise RuntimeError(f"FFmpeg não encontrado: ele é necessário para gerar {FORMATOS[fmt]['ext']}. "
                           "Instale o FFmpeg ou escolha MP3.")
    if any(not a.lower().endswith(".mp3") for a in arquivos):
        raise RuntimeError("FFmpeg não encontrado: parte deste livro foi narrada antes com o FFmpeg instalado. "
                           "Instale o FFmpeg de novo ou converta numa pasta nova.")


def _juntar_mp3_sem_ffmpeg(saida: str, fmt: str, capitulos: List[Dict[str, Any]], ao_progresso=None) -> None:
    """Sem FFmpeg, o MP3 sai juntando os capítulos como vieram da voz (mesmo formato em todos, então o
    resultado é um MP3 válido). Capítulos e capa entram depois pelas tags ID3. Fica sem o ajuste de volume."""
    _sem_ffmpeg_so_mp3(fmt, [c["file"] for c in capitulos])
    with open(saida, "wb") as destino:
        for n, ch in enumerate(capitulos, start=1):
            with open(ch["file"], "rb") as origem:
                shutil.copyfileobj(origem, destino)
            if ao_progresso:
                ao_progresso(n / len(capitulos))


def codificar_capitulo(entrada: str, saida: str, fmt: str, q: str) -> None:
    """Um arquivo por capítulo: codifica o FLAC do cache no formato final."""
    if not ffmpeg_disponivel():  # MP3 por capítulo sem FFmpeg: a voz como veio
        _sem_ffmpeg_so_mp3(fmt, [entrada])
        shutil.copyfile(entrada, saida)
        return
    res = _rodar(["ffmpeg", "-y", "-i", entrada, "-map_metadata", "-1"] + argumentos_codec(fmt, q) + [saida])
    if res.returncode != 0:
        raise RuntimeError(f"FFmpeg falhou ao gerar o capítulo: {res.stderr[-300:]}")
