"""
Metadados de um e-book para o app: título, autor, idioma, capa, capítulos (com o texto) e estimativas.
Uso: python extract_metadata.py <arquivo>  ->  imprime um JSON
"""
import base64
import json
import os
import re
import sys

import mobi
from bs4 import BeautifulSoup
from pypdf import PdfReader


def normalize_language_name(lang_str):
    if not lang_str:
        return "Português (Brasil)"
    cleaned = str(lang_str).strip().lower().replace("_", "-")
    if cleaned in ["unknown", "desconhecido", "und", "", "none"]:
        return "Português (Brasil)"
    if cleaned in ["pt", "pt-br", "br", "por", "portugues", "português", "pt-brasil", "portugues (brasil)", "português (brasil)"]:
        return "Português (Brasil)"
    if cleaned in ["pt-pt", "portugues (portugal)", "português (portugal)"]:
        return "Português (Portugal)"
    if cleaned in ["en", "en-us", "eng", "english", "ingles", "inglês"]:
        return "Inglês"
    if cleaned in ["en-gb", "english (uk)", "inglês (reino unido)"]:
        return "Inglês (Reino Unido)"
    if cleaned in ["es", "es-es", "es-la", "es-mx", "es-ar", "spa", "espanhol", "español", "spanish"]:
        return "Espanhol"
    if cleaned in ["fr", "fra", "fre", "frances", "francês", "français", "french"]:
        return "Francês"
    if cleaned in ["de", "deu", "ger", "alemao", "alemão", "deutsch", "german"]:
        return "Alemão"
    if cleaned in ["it", "ita", "italiano", "italian"]:
        return "Italiano"
    if cleaned in ["ru", "rus", "russo", "russian"]:
        return "Russo"
    if cleaned in ["ja", "jpn", "japones", "japonês", "japanese"]:
        return "Japonês"
    if cleaned in ["zh", "chi", "zho", "chines", "chinês", "chinese"]:
        return "Chinês"
    if cleaned in ["ar", "ara", "arabe", "árabe", "arabic"]:
        return "Árabe"
    if cleaned.startswith("pt"):
        return "Português (Brasil)"
    if cleaned.startswith("en"):
        return "Inglês"
    if cleaned.startswith("es"):
        return "Espanhol"
    return str(lang_str).strip().title()


def detect_sample_language(text):
    if not text or len(text.strip()) < 20:
        return ""
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        res = detect(text.strip())
        if res:
            return res
    except Exception:
        pass
    words = text.lower().split()
    pt_markers = {'que', 'não', 'uma', 'para', 'com', 'como', 'mais', 'pela', 'pelo', 'está', 'este', 'essa', 'muito', 'quando'}
    en_markers = {'the', 'and', 'that', 'have', 'with', 'this', 'from', 'they', 'will', 'would', 'there', 'their', 'about', 'which'}
    es_markers = {'los', 'las', 'del', 'por', 'con', 'como', 'más', 'pero', 'cuando', 'esta', 'muy', 'también'}
    pt, en, es = (sum(1 for w in words if w in m) for m in (pt_markers, en_markers, es_markers))
    if pt > en and pt > es and pt >= 2:
        return "pt-BR"
    if en > pt and en > es and en >= 2:
        return "en"
    if es > pt and es > en and es >= 2:
        return "es"
    return ""


def _lang_code(lang):
    l = (lang or "").lower()
    if l.startswith("en") or "ingl" in l:
        return "en"
    if l.startswith("es") or "espanh" in l or "españ" in l:
        return "es"
    if l.startswith("ar") or "árab" in l or "arab" in l:
        return "ar"
    if l.startswith("ru") or "russ" in l or "русск" in l:
        return "ru"
    return "pt"


def _duracao_legivel(minutos):
    if minutos >= 60:
        h, m = int(minutos // 60), int(minutos % 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{max(1, int(round(minutos)))}m"


def _capa_data_url(dados, mime):
    if not dados:
        return None
    if not (mime or "").startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(dados).decode('ascii')}"


def _titulo_autor(title, author, file_path, original_name):
    if not title or str(title).strip().lower() in ("unknown", "untitled", "sem título"):
        title = os.path.splitext(os.path.basename(original_name or file_path))[0]
        title = re.sub(r"^_?oceanofpdf\.com_?", "", title, flags=re.IGNORECASE).replace("_", " ").strip()
    author = str(author).strip()
    if not author or author.lower() == "unknown":
        author = "Desconhecido"
    return str(title).strip() or "Desconhecido", author


def _ano(texto):
    """Ano (4 dígitos) de uma data de metadados: "2015-03-01", "D:20150301...", "2015"."""
    m = re.search(r"(1[5-9]\d\d|20\d\d)", str(texto or ""))
    return m.group(1) if m else ""


def extract_metadata(file_path, original_name=None, ao_parcial=None):
    """Nunca levanta exceção: no pior caso devolve o nome do arquivo como título e nenhum capítulo
    (o conversor tenta ler o livro de novo na hora de converter). `ao_parcial` recebe logo no começo o que
    sai rápido (capa, título, autor, idioma), para o app mostrar o livro antes de montar os capítulos."""
    from book_structure import estruturar_livro, ler_epub, ler_texto_arquivo, LivroIlegivel

    ext = os.path.splitext(file_path)[1].lower()
    title = author = genre = subgenre = narrator = language = year = ""
    cover_data = None
    avisos = []
    ilegivel = ""

    try:
        if ext == '.epub':
            livro = ler_epub(file_path)
            meta = livro["meta"]
            title = (meta.get("title") or [""])[0]
            author = ", ".join(meta.get("creator") or [])
            language = (meta.get("language") or [""])[0]
            subjects = meta.get("subject") or []
            genre = subjects[0] if subjects else ""
            subgenre = subjects[1] if len(subjects) > 1 else ""
            year = _ano((meta.get("date") or [""])[0])
            if livro["cover"]:
                cover_data = _capa_data_url(*livro["cover"])
        elif ext == '.pdf':
            reader = PdfReader(file_path)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    avisos.append("PDF protegido por senha")
            meta = reader.metadata
            if meta:
                title = meta.title or ""
                author = meta.author or ""
                genre = getattr(meta, "subject", "") or ""
                year = _ano(str(meta.get("/CreationDate", "") or ""))
            try:
                language = str(reader.trailer['/Root'].get('/Lang', '') or '')
            except Exception:
                pass
        elif ext in ('.mobi', '.azw', '.azw3', '.prc'):
            temp_dir, arquivo = mobi.extract(file_path)
            try:
                if arquivo.lower().endswith('.epub'):
                    livro = ler_epub(arquivo)
                    title = (livro["meta"].get("title") or [""])[0]
                    author = ", ".join(livro["meta"].get("creator") or [])
                    language = (livro["meta"].get("language") or [""])[0]
                    year = _ano((livro["meta"].get("date") or [""])[0])
                    if livro["cover"]:
                        cover_data = _capa_data_url(*livro["cover"])
                else:
                    cab = open(arquivo, 'rb').read(200000).decode('utf-8', 'replace')
                    m = re.search(r'<title>(.*?)</title>', cab, re.IGNORECASE | re.DOTALL)
                    if m:
                        title = BeautifulSoup(m.group(1), 'html.parser').get_text().strip()
            finally:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
    except LivroIlegivel as e:
        ilegivel = str(e)
    except Exception as e:
        avisos.append(f"metadados: {e}")

    if ao_parcial:
        t, a = _titulo_autor(title, author, file_path, original_name)
        try:
            ao_parcial({"partial": True, "title": t, "author": a, "genre": str(genre).strip(), "subgenre": str(subgenre).strip(), "year": year,
                        "language": normalize_language_name(language) if language else "", "cover": cover_data})
        except Exception:
            pass

    # Capítulos e texto
    chapters_data = []
    char_count = 0
    try:
        caps = estruturar_livro(file_path, _lang_code(language))
        textos = [c["conteudo"] for c in caps]
        # Amostra do meio do livro (o começo costuma ter créditos e dedicatória) para detectar o idioma
        junto = "\n\n".join(textos)
        meio = len(junto) // 2
        amostra = junto[max(0, meio - 2500):meio + 2500] or junto[:5000]
        if not language:
            detectado = detect_sample_language(amostra)
            if detectado and _lang_code(detectado) != "pt":
                caps = estruturar_livro(file_path, _lang_code(detectado))  # títulos genéricos no idioma do livro
            language = detectado
        for idx, c in enumerate(caps):
            conteudo = c["conteudo"].strip()
            char_count += len(conteudo) + len(c["titulo"])
            chapters_data.append({
                "id": f"cap-{idx + 1}",
                "title": c["titulo"],
                "snippet": conteudo[:500] + ("..." if len(conteudo) > 500 else ""),
                "content": conteudo,
            })
    except LivroIlegivel as e:
        ilegivel = ilegivel or str(e)
    except Exception as e:
        avisos.append(f"capítulos: {e}")
        if ext == '.txt':
            try:
                texto = ler_texto_arquivo(file_path)
                char_count = len(texto)
                chapters_data.append({"id": "cap-1", "title": "Capítulo 1", "snippet": texto[:500], "content": texto})
            except Exception:
                pass

    title, author = _titulo_autor(title, author, file_path, original_name)

    # Estimativas (o app recalcula conforme o formato e a qualidade escolhidos)
    minutos = char_count / 900 if char_count else 0
    proc_seg = char_count / 1000 + minutos * 0.5 + 15 if char_count else 0
    tamanho_mb = minutos * 60 * 192 / 8 / 1000

    return {
        "title": str(title).strip() or "Desconhecido",
        "author": author,
        "genre": str(genre).strip(),
        "subgenre": str(subgenre).strip(),
        "year": year,
        "narrator": str(narrator).strip(),
        "language": normalize_language_name(language),
        "charCount": int(char_count),
        "durationEst": _duracao_legivel(minutos) if char_count else "",
        "processingTimeEst": (f"{int(proc_seg // 60)}m {int(proc_seg % 60)}s" if proc_seg >= 60 else f"{int(proc_seg)}s") if char_count else "",
        "audioSizeEst": (f"{tamanho_mb:.1f} MB" if tamanho_mb < 1024 else f"{tamanho_mb / 1024:.2f} GB") if char_count else "",
        "cover": cover_data,
        "chapters": chapters_data,
        "warnings": avisos,
        "unreadable": ilegivel,
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No file path provided"}))
        sys.exit(1)
    caminho = sys.argv[1]
    if not os.path.exists(caminho):
        print(json.dumps({"error": "File not found"}))
        sys.exit(1)
    try:
        def parcial(dados):
            print(json.dumps(dados, ensure_ascii=False), flush=True)
        print(json.dumps(extract_metadata(caminho, sys.argv[2] if len(sys.argv) > 2 else None, parcial), ensure_ascii=False), flush=True)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
