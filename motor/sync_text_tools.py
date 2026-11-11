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


def _tempo_lrc(segundos: float) -> str:
    centesimos = int(round(float(segundos) * 100))
    return f"{centesimos // 6000:02d}:{(centesimos // 100) % 60:02d}.{centesimos % 100:02d}"


def _lrc_frases_e_palavras(segmentos: List[Dict[str, Any]], palavras: List[Dict[str, Any]]) -> List[str]:
    """LRC estendido: uma linha por frase e o tempo de cada palavra dentro dela ([tempo] <tempo>palavra <tempo>palavra).
    Quem não entende as marcas <...> mostra as frases do mesmo jeito."""
    linhas, p = [], 0
    ordenadas = sorted(palavras, key=lambda w: float(w["start"]))
    for i, seg in enumerate(segmentos):
        limite = float(segmentos[i + 1]["start"]) if i + 1 < len(segmentos) else float("inf")
        da_frase = []
        while p < len(ordenadas) and float(ordenadas[p]["start"]) < limite:
            da_frase.append(ordenadas[p])
            p += 1
        corpo = " ".join(f"<{_tempo_lrc(w['start'])}>{str(w.get('text', '')).strip()}" for w in da_frase)
        linhas.append(f"[{_tempo_lrc(seg['start'])}] {corpo or str(seg.get('text', '')).strip()}")
    return linhas


def build_lrc(sync_data: Dict[str, Any], title: str, author: str) -> str:
    """LRC com título e autor reais no cabeçalho: frases, palavras ou as duas, conforme o que foi gerado."""
    from ebook_audiobook_converter import export_sync_data_to_format

    header = [f"[ti:{title}]", f"[ar:{author}]", "[re:Audiobook Generator]"]
    segmentos, palavras = sync_data.get("segments") or [], sync_data.get("words") or []
    if segmentos and palavras:
        return "\n".join(header + _lrc_frases_e_palavras(segmentos, palavras)) + "\n"
    body = export_sync_data_to_format(sync_data, output_format="lrc", granularity="both")
    lines = [l for l in body.split("\n") if not re.match(r"^\[(ar|ti|re):", l)]
    return "\n".join(header + [l for l in lines if l.strip()]) + "\n"


def sync_items(sync_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sync_data.get("segments") or sync_data.get("sentences") or sync_data.get("words") or []


# ---------------------------------------------------------------------------------------------
# Refino das frases e palavras (vale para a conversão e para "Gerar texto")
# ---------------------------------------------------------------------------------------------
FRASE_MAX_PALAVRAS = 24      # acima disso a frase é dividida (de preferência em ; : — e depois em vírgula)
FRASE_MAX_CARACTERES = 160
FRASE_MIN_PALAVRAS = 3       # nenhum pedaço fica menor que isso
PAUSA_FIM_DE_FRASE_S = 0.45  # pausa na fala antes de uma maiúscula: a frase terminou mesmo sem ponto
_FIM = re.compile(r"[.!?…。！？]+[\"'”’»)\]]*$")
_PAUSA_FORTE = re.compile(r"[;:]+[\"'”’»)\]]*$")
_TRAVESSAO = ("—", "–", "―")
_ABREVIACOES = {"sr", "sra", "srta", "dr", "dra", "prof", "profa", "sto", "sta", "st", "mr", "mrs", "ms", "jr", "vs",
                "etc", "pág", "cap", "art", "fig", "vol", "nº", "n", "p", "d", "av", "gen", "cel", "ltda", "cia", "ex", "e.g", "i.e"}


def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", str(s or "")).casefold()


def _maiuscula(token: str) -> bool:
    letra = re.search(r"[^\W\d_]", token)
    return bool(letra) and letra.group(0).isupper()


def _abreviacao(token: str) -> bool:
    return token.endswith(".") and (_norm(token) in _ABREVIACOES or re.fullmatch(r"[^\W\d_]\.", token) is not None)


_ABRE = ("—", "–", "―", "«", "“", "‘", "\"", "'", "(", "[", "¿", "¡")


def _palavras_do_trecho(texto: str, palavras: List[Dict[str, Any]]):
    """Liga as palavras (com tempo) aos pedaços do texto separados por espaços e devolve (pedaços, palavras,
    índice do pedaço de cada palavra). Cada palavra mostra o texto exato do livro com a pontuação grudada nela
    ("Oi!", "assim:", "—Venha"); nenhum pedaço do texto fica de fora da linha."""
    tokens = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", texto)]
    if not tokens:
        return tokens, [dict(w) for w in palavras], [-1] * len(palavras)
    # 1) Cada palavra no pedaço seguinte que a contém; continua no mesmo pedaço só quando ele ainda tem
    #    o resto dela ("guarda-chuva" falado como "guarda" e "chuva")
    achados, atual, consumido = [], -1, 0
    for w in palavras:
        alvo = _norm(w.get("text"))
        achado = None
        if alvo and atual >= 0 and _norm(tokens[atual][2])[consumido:].startswith(alvo):
            achado = atual
            consumido += len(alvo)
        elif alvo:
            for j in range(atual + 1, min(len(tokens), atual + 9)):
                nt = _norm(tokens[j][2])
                if nt.startswith(alvo) or (alvo in nt and len(alvo) > 2):
                    achado, consumido = j, nt.find(alvo) + len(alvo)
                    break
        achados.append(achado)
        if achado is not None:
            atual = achado
    # 2) Palavras que o serviço escreveu diferente ("1999" falado por extenso): ocupam os pedaços livres
    #    entre a palavra anterior e a seguinte; sem pedaço livre, ficam com o da anterior
    i = 0
    while i < len(achados):
        if achados[i] is not None:
            i += 1
            continue
        fim_run = i
        while fim_run < len(achados) and achados[fim_run] is None:
            fim_run += 1
        antes = next((achados[x] for x in range(i - 1, -1, -1) if achados[x] is not None), -1)
        depois = achados[fim_run] if fim_run < len(achados) else len(tokens)
        livres = list(range(antes + 1, depois))
        for n, x in enumerate(range(i, fim_run)):
            achados[x] = livres[min(n, len(livres) - 1)] if livres else max(antes, 0)
        i = fim_run
    # 3) Uma palavra por pedaço; várias no mesmo pedaço: dividem o texto dele ("guarda-chuva") ou viram uma só
    novas: List[Dict[str, Any]] = []
    indices: List[int] = []
    x = 0
    while x < len(palavras):
        j = achados[x]
        grupo = [x]
        while x + len(grupo) < len(palavras) and achados[x + len(grupo)] == j:
            grupo.append(x + len(grupo))
        token = tokens[j][2]
        partes, corte, base = [], 0, token.casefold()
        for n, g in enumerate(grupo):
            bruto = str(palavras[g].get("text", "")).strip().casefold()
            pos = base.find(bruto, corte) if bruto else -1
            if pos < 0:
                partes = None
                break
            fim = len(token) if n == len(grupo) - 1 else pos + len(bruto)
            partes.append(token[corte:fim])
            corte = fim
        if partes and all(p.strip() for p in partes):
            for g, p in zip(grupo, partes):
                novas.append({**palavras[g], "text": p.strip()})
                indices.append(j)
        else:  # não dá para dividir: uma palavra só, com o tempo de todas
            novas.append({**palavras[grupo[0]], "end": palavras[grupo[-1]]["end"], "text": token})
            indices.append(j)
        x += len(grupo)
    # 4) Pedaços sem palavra: os de abertura ("—", "«") vão com a palavra seguinte; os demais, com a anterior
    usados = set(indices)
    for j, (_, _, token) in enumerate(tokens):
        if j in usados:
            continue
        anterior = max((n for n, t in enumerate(indices) if t < j), default=None)
        seguinte = min((n for n, t in enumerate(indices) if t > j), default=None)
        if seguinte is not None and (token.startswith(_ABRE) and not re.search(r"\w", token) or anterior is None):
            novas[seguinte]["text"] = f"{token} {novas[seguinte]['text']}"
        elif anterior is not None:
            novas[anterior]["text"] = f"{novas[anterior]['text']} {token}"
    return tokens, novas, indices


def _cortes_da_frase(tokens, indices, palavras, tem_tempo: bool) -> List[int]:
    """Onde a frase deve ser cortada (depois do pedaço de índice k)."""
    n = len(tokens)
    obrig = []
    inicio_palavra = {}
    fim_palavra = {}
    for i, j in enumerate(indices):
        if j >= 0:
            inicio_palavra.setdefault(j, float(palavras[i]["start"]))
            fim_palavra[j] = float(palavras[i]["end"])

    def pausa(k: int) -> float:
        if not tem_tempo or k not in fim_palavra or (k + 1) not in inicio_palavra:
            return 0.0
        return inicio_palavra[k + 1] - fim_palavra[k]

    def prioridade(k: int) -> int:
        atual, seguinte = tokens[k][2], tokens[k + 1][2]
        if _FIM.search(atual) and not _abreviacao(atual):
            return 0
        if _PAUSA_FORTE.search(atual) or seguinte.startswith(_TRAVESSAO) or atual.endswith(_TRAVESSAO):
            return 1
        if atual.endswith(","):
            return 2
        return 3

    # 1) Frases que terminaram sem que o texto fosse cortado: ponto no meio da linha, ou pausa antes de maiúscula
    for k in range(n - 1):
        atual, seguinte = tokens[k][2], tokens[k + 1][2]
        if not _maiuscula(seguinte) or _abreviacao(atual):
            continue
        if _FIM.search(atual):  # "…saiu. Ela voltou": o ponto ficou no meio da linha
            obrig.append(k)
        elif (not re.search(r"[,;:(\-—–]$", atual) and pausa(k) >= PAUSA_FIM_DE_FRASE_S
              and k + 1 >= 2 and n - (k + 1) >= 2):  # sem ponto, mas a fala parou antes da maiúscula
            obrig.append(k)

    # 2) Pedaços longos demais: corta no melhor ponto perto do meio, até todos caberem
    def dividir(a: int, b: int) -> List[int]:
        qtd = b - a + 1
        chars = tokens[b][1] - tokens[a][0]
        if qtd <= FRASE_MAX_PALAVRAS and chars <= FRASE_MAX_CARACTERES:
            return []
        candidatos = [k for k in range(a + FRASE_MIN_PALAVRAS - 1, b - FRASE_MIN_PALAVRAS + 1)]
        if not candidatos:
            return []
        meio = (a + b) / 2
        melhor = min(candidatos, key=lambda k: (prioridade(k), -pausa(k) if prioridade(k) == 3 else 0, abs(k - meio)))
        return dividir(a, melhor) + [melhor] + dividir(melhor + 1, b)

    cortes, a = [], 0
    for k in sorted(set(obrig)) + [n - 1]:
        cortes += dividir(a, k)
        if k != n - 1:
            cortes.append(k)
        a = k + 1
    return sorted(set(cortes))


def refinar_sincronia(segmentos: List[Dict[str, Any]], palavras: List[Dict[str, Any]]):
    """Deixa o texto sincronizado mais legível sem mudar os tempos da fala:
    - as palavras mostram a pontuação do texto (! ? : ; , … aspas e travessões);
    - uma frase que terminou sem ponto (pausa antes de maiúscula) ou com o ponto no meio vira duas;
    - frases longas demais são divididas em ; : — ou vírgula, sem pedaços pequenos demais.
    Devolve (segmentos, palavras) novos; as listas recebidas não são alteradas."""
    segmentos = [dict(s) for s in segmentos or []]
    palavras = sorted((dict(w) for w in palavras or []), key=lambda w: float(w["start"]))
    if not segmentos:
        return segmentos, palavras
    novos, novas_palavras, p = [], [], 0
    for i, seg in enumerate(segmentos):
        texto = re.sub(r"\s+", " ", str(seg.get("text", ""))).strip()
        ini, fim = float(seg["start"]), float(seg.get("end", seg["start"]))
        limite = float(segmentos[i + 1]["start"]) if i + 1 < len(segmentos) else float("inf")
        da_frase = []
        while p < len(palavras) and float(palavras[p]["start"]) < limite - 0.02:
            da_frase.append(palavras[p])
            p += 1
        if not texto:
            novas_palavras += da_frase
            continue
        if da_frase:
            tokens, da_frase, indices = _palavras_do_trecho(texto, da_frase)
        else:
            tokens, indices = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", texto)], []
        novas_palavras += da_frase
        cortes = _cortes_da_frase(tokens, indices, da_frase, bool(da_frase))
        if not cortes:
            novos.append({**seg, "text": texto})
            continue
        limites = [0] + [k + 1 for k in cortes] + [len(tokens)]
        for a, b in zip(limites, limites[1:]):
            if a >= b:
                continue
            trecho = texto[tokens[a][0]:tokens[b - 1][1]].strip()
            dela = [da_frase[x] for x, j in enumerate(indices) if a <= j < b]
            if dela:
                t0, t1 = float(dela[0]["start"]), float(dela[-1]["end"])
            else:  # sem o tempo das palavras: proporcional ao tamanho do texto
                total = max(1, len(texto))
                t0 = ini + (fim - ini) * tokens[a][0] / total
                t1 = ini + (fim - ini) * tokens[b - 1][1] / total
            if a == 0:
                t0 = ini
            if b == len(tokens):
                t1 = max(t1, fim)
            novos.append({**seg, "start": round(t0, 3), "end": round(max(t1, t0), 3), "text": trecho})
    return novos, novas_palavras + palavras[p:]


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
    # WAV e demais formatos: sem lugar padrão para o texto, que fica só nos arquivos separados
    return False


# Separação escolhida no app -> nome usado pelo alinhador
GRANULARIDADES = {"sentences": "sentence", "words": "word", "both": "both"}


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
    granularity: str = "both",
) -> Dict[str, Any]:
    """Gera o texto e o guarda conforme o destino. Devolve o resumo do que foi feito.
    `granularity`: "sentences", "words" ou "both" (os mesmos valores da conversão)."""
    from ebook_audiobook_converter import EbookAudiobookConverter, export_sync_data_to_format

    if destination not in DESTINATIONS:
        destination = "both"
    if not audio_path or not Path(audio_path).is_file():
        raise ValueError("Arquivo de áudio do audiobook não encontrado.")
    if granularity not in GRANULARIDADES:
        granularity = "both"

    converter = EbookAudiobookConverter()
    sync_data = converter.convert(
        ebook_path=ebook_path or None,
        audio_filename=Path(audio_path).name,
        audio_path=audio_path,
        granularity=GRANULARIDADES[granularity],
        output_format="json",
        ai_filter=True,
        include_metadata=True,
        custom_chapters=custom_chapters,
        log_callback=log,
    )
    # Frases legíveis e palavras com pontuação, como na conversão
    if sync_data.get("segments"):
        try:
            segmentos, palavras = refinar_sincronia(sync_data["segments"], sync_data.get("words") or [])
            sync_data["segments"] = segmentos
            if sync_data.get("words"):
                sync_data["words"] = palavras
        except Exception as e:  # o refino é um extra: sem ele, o texto sai como veio do alinhamento
            if log:
                log(f"Aviso: frases do texto sincronizado não refinadas: {e}")
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
    parser.add_argument("--granularity", choices=list(GRANULARIDADES), default="both")
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
            granularity=args.granularity,
        )
        print(json.dumps({"type": "result", **result}, ensure_ascii=False), flush=True)
    except Exception as e:
        print(json.dumps({"type": "result", "success": False, "error": str(e)}, ensure_ascii=False), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
