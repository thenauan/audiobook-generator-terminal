"""
Estrutura um livro (Epub, Pdf, Mobi/Azw, Txt) em capítulos limpos para narração.

Fontes de capítulos, da mais confiável para a menos:
  1. Sumário do próprio livro (EPUB: nav/NCX, inclusive âncoras dentro do arquivo; PDF: marcadores).
  2. Títulos marcados no HTML (h1/h2) quando o sumário não existe ou é pobre.
  3. Heurística sobre os parágrafos ("Capítulo 3", "Chapter IV", "Prólogo", números em sequência).

Também remove o que não deve ser narrado: capa, créditos/copyright, sumário, notas de rodapé,
cabeçalhos/rodapés repetidos e números de página do PDF.

Uso:
    from book_structure import estruturar_livro, preparar_para_narracao, titulo_falado
    capitulos = estruturar_livro(caminho)          # [{"titulo": ..., "conteudo": ...}, ...]
"""
from __future__ import annotations

import html as html_lib
import os
import posixpath
import re
import shutil
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---------------------------------------------------------------------------------------------
# Vocabulário
# ---------------------------------------------------------------------------------------------
# Títulos que indicam páginas que não devem ser narradas
FRONT_DROP = re.compile(
    r"^(capa|cover|portada|cubierta|copyright|cr[ée]ditos?|p[áa]gina de cr[ée]ditos|ficha catalogr[áa]fica|"
    r"sum[áa]rio|[íi]ndice|contents|table of contents|contenido|tabla de contenido|folha de rosto|"
    r"rosto|title ?page|half ?title|frontispiece|outras obras|tamb[ée]m de|also by|other books by|"
    r"cr[ée]ditos? da edi[çc][ãa]o|landmarks|guide|dlivros|e-?livros|lelivros|oceanofpdf|"
    r"bibliografia|bibliography|bibliograf[íi]a|refer[êe]ncias(?: bibliogr[áa]ficas)?|references|works cited|"
    r"[íi]ndice remissivo|[íi]ndice onom[áa]stico|index|colof[ãa]o|colophon|notas|notes|endnotes|"
    r"содержание|оглавление|выходные данные|авторские права|примечания|библиография|список литературы|указатель)\b",
    re.IGNORECASE,
)
COPYRIGHT_HINT = re.compile(
    r"ISBN|©|copyright|todos os direitos reservados|all rights reserved|derechos reservados|все права защищены|"
    r"dados internacionais de cataloga|ficha catalogr|impresso no brasil|printed in|project gutenberg|gutenberg\.org|"
    r"baixe livros|dlivros|lelivros|le livros|oceanofpdf|e-livros\.|livros de forma r[áa]pida",
    re.IGNORECASE,
)
TOC_TITLE = re.compile(r"^(sum[áa]rio|[íi]ndice|contents|table of contents|contenido|tabla de contenidos?|содержание|оглавление)$", re.IGNORECASE)

ROMANO = r"(?=[IVXLCDMivxlcdm])(?:M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))"
NUM_EXTENSO = (
    r"(?:um|uma|dois|duas|tr[êe]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|catorze|quatorze|quinze|"
    r"dezesseis|dezessete|dezoito|dezenove|vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem|"
    r"primeiro|segundo|terceiro|quarto|quinto|sexto|s[ée]timo|oitavo|nono|d[ée]cimo|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|first|second|third|"
    r"uno|dos|cuatro|siete|ocho|nueve|diez|once|doce|trece|catorce|quince|veinte|treinta|cuarenta|cincuenta|"
    r"один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|одиннадцать|двенадцать|двадцать|тридцать|"
    r"перв(?:ая|ый|ое)|втор(?:ая|ой|ое)|трет(?:ья|ий|ье)|четв[её]рт(?:ая|ый)|пят(?:ая|ый)|шест(?:ая|ой)|седьм(?:ая|ой)|"
    r"восьм(?:ая|ой)|девят(?:ая|ый)|десят(?:ая|ый))"
)
NUM_QUALQUER = rf"(?:\d{{1,3}}|{ROMANO}|{NUM_EXTENSO}(?:[\s-]+(?:e|and|y)?\s*{NUM_EXTENSO})*)"

# Palavras que abrem um capítulo e PRECISAM de número ("Capítulo 3") ou ficar sozinhas na linha
KW_NUMERADAS = r"(?:cap[íi]tulo|cap\.|chapter|chap\.|parte|part|livro|book|libro|volume|vol\.|ato|act|acto|cena|scene|canto|глава|часть|книга|том|акт|сцена)"
# Palavras que abrem uma seção sozinhas ("Prólogo", "Epílogo"...)
KW_SOLTAS = (
    r"(?:pr[óo]logo|prologue|ep[íi]logo|epilogue|pref[áa]cio|preface|prefacio|introdu[çc][ãa]o|introduction|introducci[óo]n|"
    r"pos?f[áa]cio|posfacio|afterword|interl[úu]dio|interlude|ap[êe]ndice|appendix|conclus[ãa]o|conclusion|conclusi[óo]n|"
    r"nota do autor|nota da autora|author'?s note|nota del autor|agradecimentos|acknowledg(?:e)?ments|agradecimientos|"
    r"dedicat[óo]ria|dedication|ep[íi]grafe|epigraph|gloss[áa]rio|glossary|pr[óo]logo del autor|"
    r"пролог|эпилог|предисловие|введение|заключение|послесловие|от автора|благодарности|приложение|посвящение|эпиграф)"
)
RE_TITULO_NUMERADO = re.compile(rf"^\s*{KW_NUMERADAS}\s*[.:\-–—]?\s*({NUM_QUALQUER})\b\s*[.:\-–—]?\s*(.*)$", re.IGNORECASE)
RE_TITULO_KW_SO = re.compile(rf"^\s*{KW_NUMERADAS}\s*$", re.IGNORECASE)
RE_TITULO_SOLTO = re.compile(rf"^\s*{KW_SOLTAS}\b\s*[.:\-–—]?\s*(.*)$", re.IGNORECASE)
RE_SO_NUMERO = re.compile(rf"^\s*(\d{{1,3}}|{ROMANO})\s*[.)\-–—]?\s*$", re.IGNORECASE)

# Títulos genéricos (capítulos sem título) no idioma do livro. Idioma fora desta lista: só o número,
# que a voz lê no próprio idioma (melhor que uma palavra em português numa narração em outra língua).
PALAVRA_CAPITULO = {"pt": "Capítulo", "en": "Chapter", "es": "Capítulo", "ar": "الفصل", "ru": "Глава",
                    "fr": "Chapitre", "de": "Kapitel", "it": "Capitolo", "nl": "Hoofdstuk", "pl": "Rozdział",
                    "tr": "Bölüm", "el": "Κεφάλαιο", "sv": "Kapitel", "da": "Kapitel", "nb": "Kapittel",
                    "fi": "Luku", "cs": "Kapitola", "sk": "Kapitola", "ro": "Capitolul", "hu": "Fejezet",
                    "uk": "Розділ", "ja": "第", "zh": "第", "ko": "제", "hi": "अध्याय", "id": "Bab", "vi": "Chương"}
PALAVRA_PARTE = {"pt": "Parte", "en": "Part", "es": "Parte", "ar": "الجزء", "ru": "Часть", "fr": "Partie",
                 "de": "Teil", "it": "Parte", "nl": "Deel", "pl": "Część", "tr": "Kısım", "el": "Μέρος", "sv": "Del",
                 "da": "Del", "nb": "Del", "fi": "Osa", "cs": "Část", "ro": "Partea", "hu": "Rész", "uk": "Частина",
                 "hi": "भाग", "id": "Bagian", "vi": "Phần"}
PALAVRA_INICIO = {"pt": "Início", "en": "Opening", "es": "Inicio", "ar": "البداية", "ru": "Начало", "fr": "Début",
                  "de": "Anfang", "it": "Inizio", "nl": "Begin", "pl": "Początek", "tr": "Başlangıç", "el": "Αρχή",
                  "sv": "Början", "da": "Begyndelse", "nb": "Begynnelse", "fi": "Alku", "cs": "Začátek",
                  "ro": "Început", "hu": "Kezdet", "uk": "Початок", "hi": "आरंभ", "id": "Awal", "vi": "Mở đầu"}
# Idiomas em que o número vem no meio da palavra ("第3章", "제3장")
CAPITULO_CJK = {"ja": "第{n}章", "zh": "第{n}章", "ko": "제{n}장"}


def titulo_generico(lang: str, n: int) -> str:
    """Título de um capítulo sem título no idioma do livro (ou só o número, se o idioma não está aqui)."""
    if lang in CAPITULO_CJK:
        return CAPITULO_CJK[lang].format(n=n)
    palavra = PALAVRA_CAPITULO.get(lang)
    return f"{palavra} {n}" if palavra else str(n)

BLOCOS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "dd", "dt", "td", "th",
    "figcaption", "section", "article", "header", "footer", "aside", "tr", "table", "ul", "ol", "dl",
    "center", "address", "hr", "body", "main", "figure",
}
IGNORAR = {"script", "style", "head", "title", "svg", "math", "img", "image", "video", "audio", "object", "noscript", "iframe"}
MARCA_ANCORA = "\u0000A:"  # marcador de âncora inserido no texto (posição de uma entrada do sumário)


# ---------------------------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------------------------
def _norm_espacos(t: str) -> str:
    t = t.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    t = t.replace("­", "").replace("﻿", "").replace("​", "").replace("‌", "").replace("‍", "")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return t.strip()


def _chave(t: str) -> str:
    """Texto comparável: sem acentos, minúsculo, só letras (de qualquer alfabeto) e números."""
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", t.lower()).strip()


def romano_para_int(s: str) -> Optional[int]:
    s = (s or "").upper().strip()
    if not s or not re.fullmatch(ROMANO, s, re.IGNORECASE):
        return None
    valores = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, anterior = 0, 0
    for c in reversed(s):
        v = valores[c]
        total = total - v if v < anterior else total + v
        anterior = max(anterior, v)
    return total or None


def _valor_numero(s: str) -> Optional[int]:
    s = (s or "").strip().rstrip(".)-–—").strip()
    if s.isdigit():
        return int(s)
    return romano_para_int(s)


def _parece_maiusculo(t: str) -> bool:
    letras = [c for c in t if c.isalpha()]
    return len(letras) >= 4 and sum(c.isupper() for c in letras) / len(letras) > 0.85


PEQUENAS = {"de", "da", "do", "das", "dos", "e", "a", "o", "as", "os", "em", "na", "no", "nas", "nos", "por", "para",
            "com", "of", "the", "and", "in", "on", "to", "for", "a", "an", "el", "la", "los", "las", "y", "del", "en", "con"}


def formatar_titulo(t: str) -> str:
    """Limpa o título; títulos em CAIXA ALTA viram 'Título Normal' (numerais romanos continuam em maiúsculas)."""
    t = _norm_espacos(t).replace("\n", " ").strip(" .:-–—|")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(?<=[A-Za-zÀ-ÿ])\d{1,2}$", "", t)
    if not _parece_maiusculo(t):
        return t
    palavras = []
    for i, p in enumerate(t.split(" ")):
        nucleo = re.sub(r"[^\wÀ-ÿ]", "", p)
        if nucleo and re.fullmatch(ROMANO, nucleo.upper()) and len(nucleo) <= 6 and nucleo.upper() not in {"DI", "MI", "MD", "DC"}:
            palavras.append(p.upper())
        elif i > 0 and p.lower() in PEQUENAS and not re.search(r"[.:!?—–-]$", palavras[-1]):
            palavras.append(p.lower())
        else:
            palavras.append(p[:1].upper() + p[1:].lower())
    return " ".join(palavras)


def titulo_falado(titulo: str) -> str:
    """Versão do título para o narrador: numerais romanos de capítulo viram números ("Capítulo IV" -> "Capítulo 4")."""
    t = titulo.strip()

    def troca(m):
        n = romano_para_int(m.group(2))
        return f"{m.group(1)}{n}" if n else m.group(0)

    t = re.sub(rf"(\b{KW_NUMERADAS}\s+)({ROMANO})\b", troca, t, flags=re.IGNORECASE)
    if RE_SO_NUMERO.match(t):
        n = _valor_numero(t)
        if n:
            t = str(n)
    return t


# ---------------------------------------------------------------------------------------------
# HTML -> blocos (parágrafos, títulos e âncoras)
# ---------------------------------------------------------------------------------------------
def _eh_nota(tag: Tag) -> bool:
    tipo = " ".join(filter(None, [tag.get("epub:type", ""), tag.get("role", ""), " ".join(tag.get("class", []) or [])])).lower()
    return any(k in tipo for k in ("footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote", "noteref", "doc-noteref", "nota-rodape", "notas"))


def _blocos_html(soup: BeautifulSoup) -> List[Tuple[str, str]]:
    """Percorre o HTML em ordem e devolve [(tipo, texto)] com tipo 'p', 'h1'..'h6' ou 'anchor'."""
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for t in soup.find_all(["aside"]) + [t for t in soup.find_all(True) if _eh_nota(t)]:
        if t.parent is not None:
            t.decompose()
    # Chamadas de nota sobrescritas: <sup><a>1</a></sup>, <a><sup>2</sup></a>
    for sup in soup.find_all("sup"):
        if re.fullmatch(r"\s*[\[(]?\s*(\d{1,3}|[*†‡a-z])\s*[\])]?\s*", sup.get_text() or ""):
            sup.decompose()

    saida: List[Tuple[str, str]] = []
    atual: List[str] = []

    def despejar(tipo: str = "p"):
        texto = "".join(atual)
        atual.clear()
        partes = texto.split(MARCA_ANCORA)
        for i, parte in enumerate(partes):
            if i > 0:
                ancora, _, parte = parte.partition("\u0000")
                saida.append(("anchor", ancora))
            parte = _norm_espacos(parte)
            if parte:
                saida.append((tipo, parte))

    def andar(no):
        for filho in list(no.children):
            if isinstance(filho, NavigableString):
                atual.append(str(filho))
                continue
            if not isinstance(filho, Tag):
                continue
            nome = (filho.name or "").lower().split(":")[-1]
            if nome in IGNORAR:
                continue
            if nome == "br":
                atual.append("\n")
                continue
            if nome in BLOCOS:
                despejar()
                andar(filho)
                despejar(nome if re.fullmatch(r"h[1-6]", nome) else "p")
            else:
                andar(filho)

    corpo = soup.body or soup
    andar(corpo)
    despejar()
    return saida


def _marcar_ancoras(soup: BeautifulSoup, ids: List[str]) -> None:
    for i in ids:
        el = soup.find(id=i) or soup.find(attrs={"name": i})
        if el is not None:
            el.insert_before(NavigableString(f"{MARCA_ANCORA}{i}\u0000"))


def _soup(conteudo) -> BeautifulSoup:
    if isinstance(conteudo, bytes):
        try:
            conteudo = conteudo.decode("utf-8")
        except UnicodeDecodeError:
            conteudo = conteudo.decode("cp1252", errors="replace")
    conteudo = re.sub(r"<\?xml[^>]*\?>", "", conteudo, count=1)
    try:
        return BeautifulSoup(conteudo, "lxml")
    except Exception:
        return BeautifulSoup(conteudo, "html.parser")


# ---------------------------------------------------------------------------------------------
# EPUB (leitor próprio: aguenta arquivos com itens faltando no manifesto)
# ---------------------------------------------------------------------------------------------
NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


def _ler_zip(z: zipfile.ZipFile, caminho: str) -> Optional[bytes]:
    try:
        return z.read(caminho)
    except KeyError:
        # Alguns livros usam maiúsculas/minúsculas ou codificação diferentes no caminho
        alvo = unquote(caminho).lower()
        for n in z.namelist():
            if unquote(n).lower() == alvo:
                return z.read(n)
    return None


class LivroIlegivel(ValueError):
    """O arquivo não tem texto que dê para narrar (DRM, PDF escaneado, arquivo corrompido).
    `codigo` (e `args_codigo`) identificam o motivo sem depender do texto, para outras interfaces traduzirem."""

    def __init__(self, mensagem, codigo=None, *args_codigo):
        super().__init__(mensagem)
        self.codigo = codigo
        self.args_codigo = list(args_codigo)


# Ofuscação de fontes é comum e inofensiva; qualquer outra cifra no EPUB é DRM
_CIFRAS_DE_FONTE = ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC")


def _tem_drm(z: zipfile.ZipFile) -> bool:
    dados = _ler_zip(z, "META-INF/encryption.xml")
    if not dados:
        return False
    try:
        raiz = ET.fromstring(dados)
    except ET.ParseError:
        return True
    for enc in raiz.iter():
        if enc.tag.endswith("EncryptionMethod"):
            alg = enc.get("Algorithm", "")
            if alg and alg not in _CIFRAS_DE_FONTE:
                return True
    return False


def ler_epub(caminho: str) -> Dict:
    """Lê o EPUB: metadados, documentos na ordem de leitura e sumário (achatado, com profundidade)."""
    try:
        zf = zipfile.ZipFile(caminho)
    except zipfile.BadZipFile:
        raise LivroIlegivel("Este EPUB está corrompido ou incompleto (não é um arquivo ZIP válido).", "err_epub_corrupt")
    with zf as z:
        if _tem_drm(z):
            raise LivroIlegivel("Este EPUB tem proteção DRM e não pode ser lido. Use uma cópia sem proteção do livro.", "err_epub_drm")
        opf_path = None
        container = _ler_zip(z, "META-INF/container.xml")
        if container:
            raiz = ET.fromstring(container)
            rf = raiz.find(".//c:rootfile", NS)
            if rf is not None:
                opf_path = rf.get("full-path")
        if not opf_path:
            opf_path = next((n for n in z.namelist() if n.lower().endswith(".opf")), None)
        if not opf_path:
            raise ValueError("EPUB sem arquivo OPF")
        opf_dir = posixpath.dirname(opf_path)
        opf = ET.fromstring(_ler_zip(z, opf_path))

        def junta(base, href):
            return posixpath.normpath(posixpath.join(base, unquote(href.split("#")[0]))) if href else ""

        meta = {}
        md = opf.find("opf:metadata", NS)
        if md is not None:
            for campo in ("title", "creator", "language", "subject", "description", "date"):
                vals = [(e.text or "").strip() for e in md.findall(f"dc:{campo}", NS) if (e.text or "").strip()]
                meta[campo] = vals
        manifesto = {}
        cover_id = None
        if md is not None:
            for m in md.findall("opf:meta", NS):
                if m.get("name") == "cover":
                    cover_id = m.get("content")
        for item in opf.findall("opf:manifest/opf:item", NS):
            manifesto[item.get("id")] = {
                "href": junta(opf_dir, item.get("href", "")),
                "type": item.get("media-type", ""),
                "props": item.get("properties", "") or "",
            }
        capa = None
        cand = [v for v in manifesto.values() if "cover-image" in v["props"]]
        if cover_id and cover_id in manifesto:
            cand.insert(0, manifesto[cover_id])
        if not cand:
            cand = [v for k, v in manifesto.items() if v["type"].startswith("image/") and "cover" in (k + v["href"]).lower()]
        for c in cand:
            dados = _ler_zip(z, c["href"])
            if dados:
                capa = (dados, c["type"] or "image/jpeg")
                break

        spine_el = opf.find("opf:spine", NS)
        docs = []
        for ref in (spine_el.findall("opf:itemref", NS) if spine_el is not None else []):
            it = manifesto.get(ref.get("idref"))
            if not it or "html" not in it["type"] and not it["href"].lower().endswith((".html", ".xhtml", ".htm")):
                continue
            dados = _ler_zip(z, it["href"])
            if dados:
                docs.append({"href": it["href"], "data": dados, "nav": "nav" in it["props"]})
        if not docs:  # sem spine utilizável: todos os HTML em ordem de nome
            for n in sorted(z.namelist()):
                if n.lower().endswith((".html", ".xhtml", ".htm")):
                    docs.append({"href": n, "data": z.read(n), "nav": False})

        # Sumário: EPUB3 (nav) ou EPUB2 (NCX)
        toc: List[Dict] = []
        nav_item = next((v for v in manifesto.values() if "nav" in v["props"]), None)
        if nav_item:
            dados = _ler_zip(z, nav_item["href"])
            if dados:
                s = _soup(dados)
                navs = s.find_all("nav")
                nav = next((n for n in navs if "toc" in (n.get("epub:type", "") + n.get("role", "")).lower()), navs[0] if navs else None)
                base = posixpath.dirname(nav_item["href"])

                def percorre(ol, prof):
                    for li in ol.find_all("li", recursive=False):
                        a = li.find(["a", "span"], recursive=False) or li.find("a")
                        if a is not None:
                            href = a.get("href", "")
                            toc.append({"title": _norm_espacos(a.get_text(" ")), "path": junta(base, href) if href else "",
                                        "frag": unquote(href.split("#", 1)[1]) if "#" in href else None, "depth": prof})
                        sub = li.find(["ol", "ul"], recursive=False)
                        if sub is not None:
                            percorre(sub, prof + 1)

                if nav is not None:
                    raiz_ol = nav.find(["ol", "ul"])
                    if raiz_ol is not None:
                        percorre(raiz_ol, 0)
        if not toc and spine_el is not None and spine_el.get("toc") in manifesto:
            ncx_item = manifesto[spine_el.get("toc")]
            dados = _ler_zip(z, ncx_item["href"])
            if dados:
                raiz = ET.fromstring(dados)
                base = posixpath.dirname(ncx_item["href"])

                def nav_points(el, prof):
                    for np_ in el.findall("ncx:navPoint", NS):
                        rot = np_.find("ncx:navLabel/ncx:text", NS)
                        cont = np_.find("ncx:content", NS)
                        href = cont.get("src", "") if cont is not None else ""
                        toc.append({"title": _norm_espacos(rot.text or "") if rot is not None else "",
                                    "path": junta(base, href), "frag": unquote(href.split("#", 1)[1]) if "#" in href else None,
                                    "depth": prof})
                        nav_points(np_, prof + 1)

                nm = raiz.find("ncx:navMap", NS)
                if nm is not None:
                    nav_points(nm, 0)
    return {"meta": meta, "docs": docs, "toc": [t for t in toc if t["path"]], "cover": capa}


def _doc_eh_sumario(blocos: List[Tuple[str, str]], qtd_links: int) -> bool:
    textos = [t for k, t in blocos if k != "anchor"]
    if not textos:
        return False
    if TOC_TITLE.match(textos[0].strip()):
        return True
    total = sum(len(t) for t in textos)
    return qtd_links >= 5 and total / max(1, qtd_links) < 60


def _capitulos_epub(caminho: str, idioma: str) -> Tuple[List[Dict], Dict]:
    livro = ler_epub(caminho)
    docs, toc = livro["docs"], livro["toc"]
    titulo_livro = (livro["meta"].get("title") or [""])[0]

    # Entradas do sumário que apontam para documentos do livro (uma por posição)
    caminhos = {d["href"] for d in docs}
    entradas, vistos = [], set()
    for e in toc:
        if e["path"] not in caminhos:
            continue
        entradas.append(dict(e))

    # Várias entradas no mesmo arquivo sem âncora: procura cada título nos h1-h4 do arquivo
    soups = {d["href"]: _soup(d["data"]) for d in docs}
    por_doc: Dict[str, List[Dict]] = {}
    for e in entradas:
        por_doc.setdefault(e["path"], []).append(e)
    novo = 0
    for caminho_doc, lista in por_doc.items():
        sem_frag = [e for e in lista if not e["frag"]]
        if len(sem_frag) < 2:
            continue
        titulos_html = soups[caminho_doc].find_all(["h1", "h2", "h3", "h4"])
        usados = set()
        for e in sem_frag:
            ce = re.sub(r"\d+$", "", _chave(e["title"])).strip()
            for h in titulos_html:
                ch = re.sub(r"\d+$", "", _chave(h.get_text(" "))).strip()
                if id(h) in usados or not ce or not ch:
                    continue
                if ch == ce or (len(ce) > 6 and (ch.startswith(ce) or ce.startswith(ch))):
                    if not h.get("id"):
                        novo += 1
                        h["id"] = f"bs-toc-{novo}"
                    e["frag"] = h["id"]
                    usados.add(id(h))
                    break
    # Cada posição fica com uma entrada só (a última, normalmente a mais específica)
    unicas, vistos = [], set()
    for e in entradas:
        chave = (e["path"], e["frag"])
        if chave in vistos:
            unicas = [x for x in unicas if (x["path"], x["frag"]) != chave]
        vistos.add(chave)
        unicas.append(e)
    entradas = unicas
    usa_sumario = len(entradas) >= 2

    frags_por_doc: Dict[str, List[str]] = {}
    for e in entradas:
        if e["frag"]:
            frags_por_doc.setdefault(e["path"], []).append(e["frag"])
    inicio_por_pos = {(e["path"], e["frag"]): e for e in entradas}

    # Sequência única de eventos do livro inteiro
    eventos: List[Tuple[str, str]] = []
    for d in docs:
        s = soups[d["href"]]
        qtd_links = len(s.find_all("a", href=True))
        _marcar_ancoras(s, frags_por_doc.get(d["href"], []))
        blocos = _blocos_html(s)
        if d["nav"] or _doc_eh_sumario(blocos, qtd_links):
            # O sumário não é narrado, mas suas âncoras podem marcar inícios
            eventos.append(("doc", d["href"]))
            eventos.extend(b for b in blocos if b[0] == "anchor")
            eventos.append(("skipdoc", d["href"]))
            continue
        eventos.append(("doc", d["href"]))
        eventos.extend(blocos)

    capitulos: List[Dict] = []
    atual = {"titulo": None, "blocos": [], "doc": None}
    pulando = False
    for tipo, valor in eventos:
        if tipo == "skipdoc":
            pulando = False
            continue
        inicio = None
        if tipo == "doc":
            inicio = inicio_por_pos.get((valor, None))
            pulando = False
        elif tipo == "anchor":
            doc_atual = atual["doc"]
            inicio = next((e for (p, f), e in inicio_por_pos.items() if f == valor and p == doc_atual), None)
        if tipo == "doc":
            atual["doc"] = valor
        if inicio is not None and usa_sumario:
            capitulos.append(atual)
            atual = {"titulo": inicio["title"], "blocos": [], "doc": atual["doc"], "depth": inicio["depth"]}
            continue
        if tipo in ("doc", "anchor") or pulando:
            continue
        atual["blocos"].append((tipo, valor))
    capitulos.append(atual)

    if not usa_sumario:
        blocos = [b for c in capitulos for b in c["blocos"]]
        return _capitulos_por_titulos(blocos, idioma, titulo_livro), livro
    return _finalizar(capitulos, idioma, titulo_livro), livro


# ---------------------------------------------------------------------------------------------
# Capítulos a partir de blocos sem sumário
# ---------------------------------------------------------------------------------------------
def _candidato_titulo(texto: str, eh_h: bool) -> Optional[Tuple[str, Optional[int]]]:
    """(título, número) se o parágrafo parece abrir um capítulo; None caso contrário."""
    t = texto.strip()
    if not t or len(t) > 90 or "\n" in t.strip() and len(t.split("\n")) > 3:
        return None
    linha = t.replace("\n", " ")
    termina_frase = bool(re.search(r"[.!?…][\"”»’)]?$", linha)) and not re.search(rf"\b{ROMANO}\.$", linha, re.IGNORECASE)
    if not eh_h and not (linha[:1].isupper() or linha[:1].isdigit() or linha[:1] in "\"“'«—–-"):
        return None
    m = RE_TITULO_NUMERADO.match(linha)
    if m:
        resto = m.group(2).strip()
        numero = _valor_numero(m.group(1))
        if resto and termina_frase and (numero is None or len(resto.split()) > 8):
            return None  # frase que começa com "Capítulo..." / "Parte dos..."
        return linha, numero
    if RE_TITULO_KW_SO.match(linha):
        return linha, None
    m = RE_TITULO_SOLTO.match(linha)
    if m and len(linha.split()) <= 8 and not (termina_frase and len(linha.split()) > 4):
        return linha, None
    if RE_SO_NUMERO.match(linha):
        return linha, _valor_numero(linha)
    if eh_h and len(linha) <= 70 and not termina_frase:
        return linha, None
    return None


def _capitulos_por_titulos(blocos: List[Tuple[str, str]], idioma: str, titulo_livro: str = "") -> List[Dict]:
    """Divide uma lista de blocos em capítulos usando títulos marcados (h1/h2) ou a heurística."""
    paragrafos = [(k, t) for k, t in blocos if k != "anchor"]
    # 1) Títulos HTML de nível alto, se houver vários
    for nivel in (("h1",), ("h1", "h2"), ("h2",)):
        idx = [i for i, (k, t) in enumerate(paragrafos) if k in nivel and len(t) <= 120]
        if len(idx) >= 3:
            caps = _cortar(paragrafos, idx)
            if sum(1 for c in caps if len(_texto(c)) > 500) >= 2:
                return _finalizar(caps, idioma, titulo_livro)
    # 2) Heurística sobre o texto
    candidatos = []
    for i, (k, t) in enumerate(paragrafos):
        c = _candidato_titulo(t, k.startswith("h"))
        if c:
            candidatos.append((i, c[1], RE_SO_NUMERO.match(t) is not None))
    # Números soltos só valem em sequência (1, 2, 3...), para não pegar páginas nem anos
    aceitos, esperado = [], None
    for i, num, so_numero in candidatos:
        if so_numero:
            if num is None:
                continue
            if esperado is None and num in (0, 1, 2):
                esperado = num
            if num == esperado:
                aceitos.append(i)
                esperado = num + 1
        else:
            aceitos.append(i)
    aceitos = _remover_sumario_textual(paragrafos, aceitos)
    if len(aceitos) >= 2:
        return _finalizar(_cortar(paragrafos, aceitos), idioma, titulo_livro)
    unico = {"titulo": None, "blocos": paragrafos}
    return _finalizar([unico], idioma, titulo_livro)


def _remover_sumario_textual(paragrafos, aceitos: List[int]) -> List[int]:
    """Sequências de 3+ títulos quase sem texto entre eles são um sumário impresso: são descartadas."""
    if len(aceitos) < 3:
        return aceitos
    descartar, run = set(), [aceitos[0]]

    def fecha(r):
        if len(r) >= 3:
            # O último título da sequência pode ser o começo real do livro (logo depois do sumário impresso)
            ultimo = r[-1]
            prox = next((i for i in aceitos if i > ultimo), len(paragrafos))
            if sum(len(t) for _, t in paragrafos[ultimo + 1:prox]) >= 500:
                r = r[:-1]
            descartar.update(r)

    for a, b in zip(aceitos, aceitos[1:]):
        entre = sum(len(t) for _, t in paragrafos[a + 1:b])
        if entre < 80:
            run.append(b)
        else:
            fecha(run)
            run = [b]
    fecha(run)
    resto = [i for i in aceitos if i not in descartar]
    # Também remove o conteúdo do sumário (os itens viram parágrafos soltos no capítulo anterior)
    for i in descartar:
        paragrafos[i] = ("toc", paragrafos[i][1])
    return resto


def _cortar(paragrafos, inicios: List[int]) -> List[Dict]:
    caps = []
    if inicios and inicios[0] > 0:
        caps.append({"titulo": None, "blocos": paragrafos[:inicios[0]]})
    for n, i in enumerate(inicios):
        fim = inicios[n + 1] if n + 1 < len(inicios) else len(paragrafos)
        caps.append({"titulo": paragrafos[i][1], "blocos": paragrafos[i + 1:fim]})
    return caps


def _texto(cap: Dict) -> str:
    return "\n\n".join(t for k, t in cap["blocos"] if k not in ("anchor", "toc"))


# ---------------------------------------------------------------------------------------------
# Acabamento: limpa títulos, tira páginas que não se narram, junta divisórias
# ---------------------------------------------------------------------------------------------
def _finalizar(capitulos: List[Dict], idioma: str, titulo_livro: str = "") -> List[Dict]:
    lang = (idioma or "pt")[:2].lower()
    chave_livro = _chave(titulo_livro)
    palavra_cap = PALAVRA_CAPITULO.get(lang, "")
    saida: List[Dict] = []
    prefixo_pendente = None

    for cap in capitulos:
        titulo = formatar_titulo(cap.get("titulo") or "")
        blocos = [(k, t) for k, t in cap["blocos"] if k not in ("anchor", "toc")]

        # Títulos repetidos no começo do conteúdo (o h1 do capítulo) saem do texto e completam o título
        extras = []
        while blocos and len(extras) < 3:
            k, t = blocos[0]
            linha = t.replace("\n", " ").strip()
            if len(linha) > 90:
                break
            chave_t, chave_l = _chave(titulo), _chave(linha)
            mesmo = chave_l and (chave_l == chave_t or chave_l in chave_t or (chave_t and chave_t in chave_l and len(chave_l) <= len(chave_t) + 40))
            if mesmo:
                if len(chave_l) > len(chave_t) and k.startswith("h"):
                    titulo = formatar_titulo(linha)
                blocos.pop(0)
                continue
            titulo_so_numero = not titulo or RE_SO_NUMERO.match(titulo) or (
                RE_TITULO_NUMERADO.match(titulo) and not RE_TITULO_NUMERADO.match(titulo).group(2).strip())
            if titulo_so_numero and k.startswith("h") and len(linha) <= 70 and not re.search(r"[.!?…]$", linha):
                extras.append(formatar_titulo(linha))
                blocos.pop(0)
                continue
            break

        # Título só com número vira "Capítulo N"; o subtítulo (h2/h3 logo abaixo) entra no título
        if RE_SO_NUMERO.match(titulo or ""):
            n = _valor_numero(titulo)
            titulo = titulo_generico(lang, n) if n is not None else (f"{palavra_cap} {titulo}" if palavra_cap else titulo)
        if extras:
            sub = " — ".join(e for e in extras if _chave(e) not in _chave(titulo))
            if sub:
                titulo = f"{titulo} — {sub}" if titulo else sub
        if not titulo and blocos:
            # Sem título: um primeiro parágrafo curto, sem ponto final, serve de título
            k, t = blocos[0]
            if _candidato_titulo(t, k.startswith("h")):
                titulo = formatar_titulo(t)
                blocos.pop(0)

        texto = limpar_texto("\n\n".join(t for _, t in blocos))

        # Páginas que não se narram
        if titulo and "gutenberg" in titulo.lower():
            continue
        if titulo and FRONT_DROP.match(_chave(titulo).replace(" ", " ")) or (titulo and FRONT_DROP.match(titulo)):
            continue
        if len(texto) < 3000 and COPYRIGHT_HINT.search(texto) and (not titulo or len(texto) < 1500 or "gutenberg" in texto.lower()):
            continue
        if chave_livro and titulo and len(texto) < 600 and (_chave(titulo) in chave_livro or chave_livro in _chave(titulo)):
            continue  # página de rosto: o título é o do livro e quase não há texto
        if len(re.sub(r"\W", "", texto)) < 25:
            # Divisória ("Parte um", "Livro II", "I – O Novo Mundo") sem texto: o título vai para o próximo capítulo.
            # Outras páginas vazias (dedicatória curta, mapa, página de rosto) simplesmente saem.
            if titulo and _eh_divisoria(titulo):
                prefixo_pendente = f"{prefixo_pendente} — {titulo}" if prefixo_pendente else titulo
            continue
        if prefixo_pendente:
            titulo = f"{prefixo_pendente} — {titulo}" if titulo else prefixo_pendente
            prefixo_pendente = None
        saida.append({"titulo": titulo, "conteudo": texto})

    # Sumário duplicado: um "capítulo" curto cujo título reaparece depois é uma entrada de sumário
    vistos_depois = {}
    for i, c in enumerate(saida):
        vistos_depois.setdefault(_chave(c["titulo"] or ""), []).append(i)
    saida = [c for i, c in enumerate(saida)
             if not (c["titulo"] and len(c["conteudo"]) < 400 and len(vistos_depois.get(_chave(c["titulo"]), [])) > 1
                     and vistos_depois[_chave(c["titulo"])][-1] != i)]

    # Sem título: abertura (antes do primeiro capítulo) ou continuação
    for i, c in enumerate(saida):
        if not c["titulo"]:
            c["titulo"] = (PALAVRA_INICIO.get(lang) or titulo_generico(lang, 1)) if i == 0 else titulo_generico(lang, i + 1)

    # Um capítulo só e muito longo: partes de ~30 mil caracteres, para dar para navegar
    if len(saida) == 1 and len(saida[0]["conteudo"]) > 60000:
        saida = _dividir_em_partes(saida[0], PALAVRA_PARTE.get(lang, ""))
    return saida


RE_DIVISORIA = re.compile(rf"^\s*(?:(?:parte|part|livro|book|libro|volume|vol\.|ato|act|acto|часть|книга|том)\b|{ROMANO}\s*[.:\-–—])", re.IGNORECASE)


def _eh_divisoria(titulo: str) -> bool:
    return bool(RE_DIVISORIA.match(titulo)) or (len(titulo) <= 60 and _parece_maiusculo(titulo) and not FRONT_DROP.match(titulo))


def _dividir_em_partes(cap: Dict, palavra: str, alvo: int = 30000) -> List[Dict]:
    paragrafos = [p for p in cap["conteudo"].split("\n\n") if p.strip()]
    partes, atual, tam = [], [], 0
    for p in paragrafos:
        atual.append(p)
        tam += len(p)
        if tam >= alvo:
            partes.append(atual)
            atual, tam = [], 0
    if atual:
        if partes and tam < alvo * 0.3:
            partes[-1].extend(atual)
        else:
            partes.append(atual)
    return [{"titulo": f"{palavra} {i}".strip(),"conteudo": "\n\n".join(p)} for i, p in enumerate(partes, start=1)]


# ---------------------------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------------------------
def _linhas_repetidas(paginas: List[List[str]]) -> set:
    """Cabeçalhos/rodapés: linhas (com números trocados por #) que se repetem no topo/base de muitas páginas."""
    if len(paginas) < 5:
        return set()
    contagem: Dict[str, int] = {}
    for linhas in paginas:
        bordas = {re.sub(r"\d+", "#", l.strip().lower()) for l in (linhas[:2] + linhas[-2:]) if l.strip()}
        for b in bordas:
            contagem[b] = contagem.get(b, 0) + 1
    limite = max(3, int(len(paginas) * 0.3))
    return {b for b, c in contagem.items() if c >= limite}


def _paragrafos_pdf(linhas: List[str]) -> List[str]:
    """Junta linhas quebradas pelo layout em parágrafos."""
    tamanhos = sorted(len(l) for l in linhas if len(l) > 20)
    mediana = tamanhos[len(tamanhos) // 2] if tamanhos else 60
    paragrafos, atual = [], ""
    for i, l in enumerate(linhas):
        l = l.strip()
        if not l:
            if atual:
                paragrafos.append(atual)
                atual = ""
            continue
        if atual.endswith(("-", "‐", "‑")) and l[:1].islower():
            atual = atual[:-1] + l
        else:
            atual = f"{atual} {l}" if atual else l
        prox = linhas[i + 1].strip() if i + 1 < len(linhas) else ""
        fim_frase = re.search(r"[.!?…:\"”»’)]$", l) is not None
        curta = len(l) < mediana * 0.75
        prox_dialogo = prox[:1] in ("—", "–", "-", "•")
        if (fim_frase and (curta or prox_dialogo)) or (curta and not prox[:1].islower() and len(l) < 70 and not fim_frase and len(l.split()) <= 12):
            paragrafos.append(atual)
            atual = ""
    if atual:
        paragrafos.append(atual)
    return paragrafos


def _capitulos_pdf(caminho: str, idioma: str) -> List[Dict]:
    from pypdf import PdfReader
    try:
        leitor = PdfReader(caminho)
    except Exception as e:
        raise LivroIlegivel(f"Não foi possível abrir este PDF ({type(e).__name__}). O arquivo pode estar corrompido.", "err_pdf_open",
                            type(e).__name__)
    if leitor.is_encrypted:
        try:
            leitor.decrypt("")
        except Exception:
            raise LivroIlegivel("Este PDF é protegido por senha. Remova a proteção antes de converter.", "err_pdf_password")
    paginas = []
    for p in leitor.pages:
        try:
            texto = p.extract_text() or ""
        except Exception:
            texto = ""
        paginas.append([l for l in texto.replace("\r", "\n").split("\n")])
    repetidas = _linhas_repetidas(paginas)
    num_pagina = re.compile(r"^\s*(?:p[áa]g(?:ina)?\.?\s*)?\d{1,4}(?:\s*(?:de|of|/)\s*\d{1,4})?\s*$", re.IGNORECASE)
    limpas = []
    for linhas in paginas:
        ls = [l for l in linhas if not num_pagina.match(l) and re.sub(r"\d+", "#", l.strip().lower()) not in repetidas]
        limpas.append(ls)

    # Marcadores (outline) do PDF, quando existem, definem os capítulos
    marcos = []
    try:
        def percorre(itens, prof):
            for it in itens:
                if isinstance(it, list):
                    percorre(it, prof + 1)
                else:
                    try:
                        marcos.append((leitor.get_destination_page_number(it), str(it.title or "").strip(), prof))
                    except Exception:
                        pass
        percorre(leitor.outline or [], 0)
    except Exception:
        marcos = []
    if marcos:
        prof_min = min(m[2] for m in marcos)
        nivel = [m for m in marcos if m[2] == prof_min]
        if len(nivel) < 3:
            nivel = [m for m in marcos if m[2] <= prof_min + 1]
        nivel = sorted({m[0]: m for m in nivel if m[0] is not None}.values())
        if len(nivel) >= 2:
            caps = []
            if nivel[0][0] > 0:
                caps.append({"titulo": None, "blocos": [("p", p) for pg in limpas[:nivel[0][0]] for p in _paragrafos_pdf(pg)]})
            for n, (pagina, titulo, _) in enumerate(nivel):
                fim = nivel[n + 1][0] if n + 1 < len(nivel) else len(limpas)
                linhas = [l for pg in limpas[pagina:fim] for l in pg + [""]]
                caps.append({"titulo": titulo, "blocos": [("p", p) for p in _paragrafos_pdf(linhas)]})
            return _finalizar(caps, idioma)

    linhas = [l for pg in limpas for l in pg + [""]]
    return _capitulos_por_titulos([("p", p) for p in _paragrafos_pdf(linhas)], idioma)


# ---------------------------------------------------------------------------------------------
# TXT e MOBI
# ---------------------------------------------------------------------------------------------
def ler_texto_arquivo(caminho: str) -> str:
    dados = open(caminho, "rb").read()
    for cod in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return dados.decode(cod)
        except UnicodeDecodeError:
            continue
    return dados.decode("utf-8", errors="replace")


def _capitulos_txt(caminho: str, idioma: str) -> List[Dict]:
    texto = ler_texto_arquivo(caminho).replace("\r\n", "\n").replace("\r", "\n")
    # Projeto Gutenberg: só o livro, sem a licença
    m_ini = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", texto, re.IGNORECASE)
    m_fim = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", texto, re.IGNORECASE)
    if m_ini:
        texto = texto[m_ini.end():m_fim.start() if m_fim else None]
    blocos_brutos = re.split(r"\n\s*\n", texto)
    if len(blocos_brutos) < 5 and texto.count("\n") > 20:
        blocos_brutos = texto.split("\n")  # um parágrafo por linha
    paragrafos = []
    for b in blocos_brutos:
        linhas = [l.strip() for l in b.split("\n") if l.strip()]
        if not linhas:
            continue
        juntas = ""
        for l in linhas:
            if juntas.endswith(("-", "‐")) and l[:1].islower():
                juntas = juntas[:-1] + l
            else:
                juntas = f"{juntas} {l}" if juntas else l
        paragrafos.append(("p", juntas))
    return _capitulos_por_titulos(paragrafos, idioma)


def _capitulos_mobi(caminho: str, idioma: str) -> List[Dict]:
    import mobi
    temp_dir, arquivo = mobi.extract(caminho)
    try:
        if arquivo.lower().endswith(".epub"):
            caps, _ = _capitulos_epub(arquivo, idioma)
            return caps
        dados = open(arquivo, "rb").read()
        texto = dados.decode("utf-8", errors="replace")
        # Quebras de página do Kindle separam capítulos: viram títulos de nível 1 quando seguidas de um título
        texto = re.sub(r"<mbp:pagebreak\s*/?>", "<hr/>", texto, flags=re.IGNORECASE)
        sopa = _soup(texto)
        for par in sopa.find_all("p"):
            fonte = par.find("font", size=True)
            conteudo = par.get_text(" ", strip=True)
            if fonte is not None and conteudo and len(conteudo) <= 120 and _chave(fonte.get_text(" ")) == _chave(conteudo):
                try:
                    tam = int(str(fonte["size"]).strip("+"))
                except ValueError:
                    continue
                if tam >= 5:
                    par.name = "h1" if tam >= 6 else "h2"
        blocos = _blocos_html(sopa)
        return _capitulos_por_titulos(blocos, idioma)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# Limpeza do texto (o que vai para o narrador e para o texto sincronizado)
# ---------------------------------------------------------------------------------------------
def limpar_texto(texto: str) -> str:
    if not texto:
        return ""
    t = texto.replace("­", "").replace("﻿", "").replace("​", "")
    # Palavras partidas por hífen no fim da linha
    t = re.sub(r"(\w)[-‐‑]\n\s*(\w)", r"\1\2", t)
    # Notas de rodapé: linhas que começam com marcador explícito de nota
    t = re.sub(r"(?m)^\s*(?:\[\s*\d{1,3}\s*\]|\(\s*\d{1,3}\s*\)|\d{1,3}\s*[.)])?\s*(?:N\.\s*(?:do|da)\s*[TEA]\.?|Nota d[oa] (?:tradutor|tradutora|editor|editora|edi[çc][ãa]o|autor|autora)|Translator'?s note|Editor'?s note|Nota del traductor)\b.*$", "", t, flags=re.IGNORECASE)
    # Chamadas de nota no meio do texto: [1], [12], [*], [a]
    t = re.sub(r"\[\s*(?:\d{1,3}|\*+|[a-z]|†|‡)\s*\]", "", t, flags=re.IGNORECASE)
    t = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", t)
    t = re.sub(r"(?<=[A-Za-zÀ-ÿ.,;:!?])\(\d{1,3}\)", "", t)
    # Endereços de internet não se narram bem
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    # Separadores de cena (***, ---, ~~~, ◆◆◆) viram uma pausa
    t = re.sub(r"(?m)^\s*(?:[*~#◆◇•·\-_=]\s*){3,}$", "…", t)
    # Travessões de fala
    t = re.sub(r"(?m)^[–—-]\s*", "— ", t)
    # Letras repetidas demais ("Aaaaaah") e espaços
    t = re.sub(r"([A-Za-zÀ-ÿ])\1{3,}", r"\1\1\1", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ---------------------------------------------------------------------------------------------
# Texto para o narrador
# ---------------------------------------------------------------------------------------------
ABREVIACOES = {
    "pt": {"Sr": "Senhor", "Sra": "Senhora", "Srta": "Senhorita", "Dr": "Doutor", "Dra": "Doutora", "Prof": "Professor",
           "Profa": "Professora", "Gen": "General", "Cel": "Coronel", "Ten": "Tenente", "Sgt": "Sargento", "Gov": "Governador",
           "Dep": "Deputado", "Sen": "Senador", "Av": "Avenida", "Pça": "Praça", "Cia": "Companhia", "Ltda": "Limitada",
           "Exmo": "Excelentíssimo", "Exma": "Excelentíssima", "Sto": "Santo", "Sta": "Santa", "pág": "página", "págs": "páginas",
           "Pág": "Página", "Vol": "Volume", "vol": "volume", "ed": "edição", "séc": "século", "Séc": "Século"},
    "en": {"Mr": "Mister", "Mrs": "Missus", "Ms": "Miz", "Dr": "Doctor", "Prof": "Professor", "St": "Saint", "Capt": "Captain",
           "Sgt": "Sergeant", "Gen": "General", "Col": "Colonel", "Lt": "Lieutenant", "Gov": "Governor", "Sen": "Senator",
           "Jr": "Junior", "Sr": "Senior", "Mt": "Mount", "Ave": "Avenue", "Blvd": "Boulevard", "Dept": "Department",
           "vs": "versus", "etc": "et cetera"},
    "es": {"Sr": "Señor", "Sra": "Señora", "Srta": "Señorita", "Dr": "Doctor", "Dra": "Doctora", "Prof": "Profesor",
           "Ud": "usted", "Uds": "ustedes", "Gral": "General", "Cap": "Capitán", "Ing": "Ingeniero", "Lic": "Licenciado",
           "Avda": "Avenida", "Sto": "Santo", "Sta": "Santa", "pág": "página", "Vol": "Volumen"},
}


def preparar_para_narracao(texto: str, voz_ou_idioma: str = "pt", pronunciation_map=None) -> str:
    """Ajustes que melhoram a leitura do narrador neural, sem inventar pausas nem acentos."""
    if not texto:
        return ""
    lang = (voz_ou_idioma or "pt").split("-")[0].lower()
    t = texto

    # Dicionário de pronúncia do usuário (palavra inteira, sem diferenciar maiúsculas)
    if pronunciation_map:
        itens = pronunciation_map.items() if isinstance(pronunciation_map, dict) else [
            (i.get("original"), i.get("replacement")) for i in pronunciation_map if isinstance(i, dict)]
        for orig, rep in itens:
            if orig and rep:
                t = re.sub(rf"(?<!\w){re.escape(str(orig).strip())}(?!\w)", str(rep).strip(), t, flags=re.IGNORECASE)

    # Pontuação que confunde a entonação
    t = t.replace("...", "…")
    t = re.sub(r"([!?])\1+", r"\1", t)
    t = re.sub(r"([!?])[!?]+", r"\1", t)
    t = re.sub(r"\.{2}(?!\.)", ".", t)
    t = t.replace("“", "\"").replace("”", "\"").replace("„", "\"")

    # Abreviações (sem acrescentar vírgulas: elas criariam pausas no meio da frase)
    abrevs = ABREVIACOES.get(lang, {})
    if abrevs:
        t = re.sub(r"\b(" + "|".join(sorted(map(re.escape, abrevs), key=len, reverse=True)) + r")\.(?=\s)",
                   lambda m: abrevs.get(m.group(1), m.group(1)), t)
    if lang == "pt":
        t = re.sub(r"\bCap\.\s*(\d+)", r"Capítulo \1", t)
        t = re.sub(r"\bn\.?\s*º\s*(\d)", r"número \1", t, flags=re.IGNORECASE)
    elif lang == "en":
        t = re.sub(r"\bCh\.\s*(\d+)", r"Chapter \1", t)
        t = re.sub(r"\bNo\.\s*(\d)", r"number \1", t)
    elif lang == "es":
        t = re.sub(r"\bCap\.\s*(\d+)", r"Capítulo \1", t)
    elif lang == "ru":
        t = re.sub(r"\bи\s+т\.\s*д\.", "и так далее", t)
        t = re.sub(r"\bи\s+т\.\s*п\.", "и тому подобное", t)
        t = re.sub(r"\bт\.\s*е\.", "то есть", t)
        t = re.sub(r"\bт\.\s*к\.", "так как", t)
        t = re.sub(r"\bгл\.\s*(\d+)", r"глава \1", t)
    elif lang == "ar":
        t = t.replace("ـ", "")

    # Cada parágrafo termina com pontuação: evita que o narrador emende um parágrafo no outro
    paragrafos = []
    for p in t.split("\n\n"):
        p = _abrir_caixa_alta_inicial(p.strip())
        if not p:
            continue
        if not re.search(r"[.!?…:;\"'”»’)\]]$", p):
            p += "."
        paragrafos.append(p)
    t = "\n\n".join(paragrafos)
    return re.sub(r"[ \t]+", " ", t).strip()


def _abrir_caixa_alta_inicial(p: str) -> str:
    """Muitos livros abrem o capítulo com as primeiras palavras em CAIXA ALTA ("DAMEN VOLTOU A si").
    Para o narrador não soletrar, essas palavras viram texto normal ("Damen voltou a si")."""
    tokens = p.split(" ")
    run = []
    for i, tk in enumerate(tokens):
        letras = re.sub(r"[\W\d_]", "", tk)
        if not letras:
            continue  # números e pontuação não interrompem a sequência
        if letras.isupper():
            run.append(i)
            continue
        break
    else:
        return p  # parágrafo inteiro em maiúsculas (título, aviso): fica como está
    if len(run) < 2 or sum(len(re.sub(r"[\W\d_]", "", tokens[i])) for i in run) < 4:
        return p
    for n, i in enumerate(run):
        tk = tokens[i].lower()
        if n == 0:
            m = re.search(r"[^\W\d_]", tk)
            if m:
                tk = tk[:m.start()] + tk[m.start()].upper() + tk[m.start() + 1:]
        tokens[i] = tk
    return " ".join(tokens)


# ---------------------------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------------------------
def estruturar_livro(caminho: str, idioma: str = "pt") -> List[Dict]:
    """[{"titulo": str, "conteudo": str}] na ordem do livro, prontos para narrar (antes da preparação da fala)."""
    ext = os.path.splitext(caminho)[1].lower()
    if ext == ".epub":
        try:
            caps, _ = _capitulos_epub(caminho, idioma)
        except LivroIlegivel:
            raise
        except Exception:
            caps = _capitulos_epub_bruto(caminho, idioma)
    elif ext == ".pdf":
        caps = _capitulos_pdf(caminho, idioma)
        if sum(len(c["conteudo"]) for c in caps) < 200:
            raise LivroIlegivel("Este PDF não tem texto selecionável (parece ser escaneado, só com imagens). "
                                "Use uma versão com texto ou passe o PDF por um programa de OCR antes.", "err_pdf_scanned")
    elif ext in (".mobi", ".azw", ".azw3", ".prc"):
        caps = _capitulos_mobi(caminho, idioma)
    else:
        caps = _capitulos_txt(caminho, idioma)
    return [c for c in caps if c["conteudo"].strip()]


def _capitulos_epub_bruto(caminho: str, idioma: str) -> List[Dict]:
    """Último recurso para EPUBs muito quebrados: todos os HTML do zip, em ordem de nome."""
    blocos = []
    with zipfile.ZipFile(caminho) as z:
        for n in sorted(z.namelist()):
            if n.lower().endswith((".html", ".xhtml", ".htm")):
                blocos.extend(_blocos_html(_soup(z.read(n))))
    return _capitulos_por_titulos(blocos, idioma)


def texto_completo(caminho: str, idioma: str = "pt") -> str:
    """Texto inteiro do livro (já limpo), para estimativas e detecção de idioma."""
    return "\n\n".join(c["conteudo"] for c in estruturar_livro(caminho, idioma))


if __name__ == "__main__":
    import json
    import sys
    caps = estruturar_livro(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "pt")
    print(json.dumps([{"titulo": c["titulo"], "chars": len(c["conteudo"]), "inicio": c["conteudo"][:80]} for c in caps],
                     ensure_ascii=False, indent=1))
