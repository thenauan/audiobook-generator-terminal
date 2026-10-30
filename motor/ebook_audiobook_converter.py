#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Conversor e Sincronizador de Textos de Ebooks para Player de Audiobook.

Suporta a extração e conversão de:
- EPUB 3 com Media Overlays (arquivos SMIL + XHTML com tempos reais de narração)
- EPUB 2/3 comuns (sem tempos embutidos, extraindo capítulos, frases e palavras)
- MOBI / AZW / PRC (descompressão nativa PalmDOC LZ77 + fallback para bibliotecas externas)
- PDF (via pypdf / pdfplumber)
- TXT (detecção automática de codificação UTF-8 / Latin-1 / CP1252)
- Formatos auxiliares de sincronização (LRC, WebVTT, SRT)
- Leitura de metadados de áudio (MP3 ID3v2 SYLT, capítulos, título e autor)

Formatos de saída JSON compatíveis com o player:
- Modo "sentence" (por frase): {"audio": "...", "segments": [{"start": 0.0, "end": 3.2, "text": "..."}]}
- Modo "word" (por palavra):   {"audio": "...", "words": [{"start": 0.0, "end": 0.35, "text": "..."}]}
- Modo "both" (ambos):         Inclui "segments" e "words" no mesmo JSON.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import html
import json
import os
from pathlib import Path
import posixpath
import re
import struct
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import xml.etree.ElementTree as ET
import zipfile

# Tentativa de importar bibliotecas opcionais presentes no ambiente
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    import mutagen
    from mutagen.id3 import ID3
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False


GranularityMode = Literal["sentence", "word", "both"]


# =============================================================================
# 1. ESTRUTURAS DE DADOS
# =============================================================================

@dataclass
class TimedItem:
    """Representa um trecho sincronizado (frase ou palavra)."""
    start: float
    end: float
    text: str
    audio_src: Optional[str] = None
    chapter: Optional[int] = None
    is_chapter_title: bool = False

    def to_dict(self, decimals: int = 2) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "start": round(float(self.start), decimals),
            "end": round(float(self.end), decimals),
            "text": self.text,
        }
        if self.chapter is not None:
            d["chapter"] = self.chapter
        if self.is_chapter_title:
            d["is_chapter_title"] = True
        return d


@dataclass
class AudioMetadataResult:
    """Resultado da inspeção de metadados de um arquivo de audiobook."""
    audio_filename: str
    title: Optional[str] = None
    author: Optional[str] = None
    duration: Optional[float] = None
    chapters: List[Dict[str, Any]] = field(default_factory=list)
    has_synchronized_text: bool = False
    sync_source: Optional[str] = None
    segments: List[TimedItem] = field(default_factory=list)
    message: str = ""


@dataclass
class ValidationReport:
    """Relatório de validação dos tempos de sincronização."""
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    total_items: int = 0
    max_end_time: float = 0.0


# =============================================================================
# 2. TOKENIZAÇÃO DE TEXTO (FRASES E PALAVRAS) E ESTIMATIVA TEMPORAL
# =============================================================================

# Abreviações comuns em português e inglês que não devem quebrar frases
COMMON_ABBREVIATIONS = {
    "sr", "sra", "srta", "dr", "dra", "prof", "profa", "eng", "pe", "st", "sto",
    "sta", "etc", "ex", "fig", "cap", "vol", "pág", "pag", "art", "inc", "ltda",
    "mr", "mrs", "ms", "jr", "vs", "ed", "no", "tel", "av", "r", "min", "seg",
}

_SENTENCE_SPLIT_REGEX = re.compile(
    r'(?<=[.!?…])\s+(?=[A-ZÁÀÂÃÉÊÍÓÔÕÚÇÜ"“«—\-0-9])'
)

_WORD_TOKEN_REGEX = re.compile(r"\S+")


def clean_text_for_ai_reader(raw_text: str) -> str:
    """
    Higieniza o texto de ebooks para reprodução e leitura ideal por IAs:
    - Desfaz quebras de linha com hífen (ex.: 'cons-\ntituição' -> 'constituição').
    - Remove números de página isolados e cabeçalhos/rodapés repetitivos.
    - Remove chamadas de notas de rodapé ([1], [12], ¹, ², *, etc.).
    - Remove URLs e hiperlinks brutos que a IA soletraria de forma truncada.
    - Remove metadados editoriais avulsos (ISBN, avisos de direitos autorais repetitivos).
    - Normaliza travessões de diálogo (—) com espaçamento adequado para respiração da IA.
    - Limpa espaços múltiplos e caracteres de controle invisíveis.
    """
    if not raw_text:
        return ""

    text = html.unescape(raw_text)

    # 1. Caracteres de controle e invisíveis (zero-width, soft hyphens, BOM)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff\u00ad]", "", text)

    # 2. Rejunta hifenizações de quebra de linha de PDFs e ebooks (ex: 'desen-\nvolvimento')
    text = re.sub(r"(\b[A-Za-zÀ-ÿ]+)-\s*[\r\n]+\s*([A-Za-zÀ-ÿ]+\b)", r"\1\2", text)

    # 3. Remove URLs e links brutos (http://..., https://..., www....)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)

    # 4. Remove linhas isoladas de numeração de página e rodapés de página
    text = re.sub(r"(?im)^\s*(?:p[aá]g(?:ina)?\.?\s*)?\d+\s*$", "", text)
    text = re.sub(r"(?im)^\s*[\w\s]{1,30}\s*\|\s*\d+\s*$", "", text)

    # 5. Remove chamadas de notas de rodapé numéricas e sobrescritas
    text = re.sub(r"\[\s*\d+\s*\]", "", text)
    text = re.sub(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+", "", text)
    text = re.sub(r"\s*[\*†‡§]\s*", " ", text)

    # 6. Remove dados de ISBN e direitos autorais repetitivos
    text = re.sub(r"(?i)\bISBN(?:-1[03])?:?\s*[\d-]+\b", "", text)
    text = re.sub(r"(?im)^\s*(?:todos os direitos reservados|copyright\s*©|dados de copyright).*$", "", text)

    # 7. Diálogos e travessões: unifica em travessão ('—') e garante espaço após
    text = re.sub(r"(?m)^[\-\u2013\u2014]\s*", "— ", text)
    text = re.sub(r"\s*[\u2013\u2014]\s*", " — ", text)

    # 8. Normaliza quebras de linha e múltiplos espaços
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)

    return text.strip()


def clean_extracted_text(raw_text: str, ai_mode: bool = True) -> str:
    """Normaliza o texto extraído, aplicando limpeza otimizada para ritmo de IA."""
    if ai_mode:
        return clean_text_for_ai_reader(raw_text)
    if not raw_text:
        return ""
    text = html.unescape(raw_text)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def format_time_lrc(seconds: float) -> str:
    """Converte segundos para timestamp LRC: [mm:ss.xx]."""
    total = max(0.0, float(seconds))
    mm = int(total // 60)
    ss = int(total % 60)
    xx = int(round((total - int(total)) * 100))
    if xx >= 100:
        ss += 1
        xx = 0
    return f"[{mm:02d}:{ss:02d}.{xx:02d}]"


def format_time_srt(seconds: float) -> str:
    """Converte segundos para timestamp SRT: hh:mm:ss,ms."""
    total = max(0.0, float(seconds))
    hh = int(total // 3600)
    rem = total % 3600
    mm = int(rem // 60)
    ss = int(rem % 60)
    ms = int(round((rem - int(rem)) * 1000))
    if ms >= 1000:
        ss += 1
        ms = 0
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


def format_time_vtt(seconds: float) -> str:
    """Converte segundos para timestamp WebVTT: hh:mm:ss.ms."""
    total = max(0.0, float(seconds))
    hh = int(total // 3600)
    rem = total % 3600
    mm = int(rem // 60)
    ss = int(rem % 60)
    ms = int(round((rem - int(rem)) * 1000))
    if ms >= 1000:
        ss += 1
        ms = 0
    return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"


def export_sync_data_to_format(
    sync_data: Dict[str, Any],
    output_format: str = "json",
    output_path: Optional[Union[str, Path]] = None,
    granularity: str = "both",
) -> str:
    """
    Exporta os dados sincronizados para o formato escolhido:
    - 'json': JSON nativo estruturado para players (segments, words, metadados)
    - 'lrc': Arquivo de sincronização LRC (compatível com 99% dos players móveis e desktop)
    - 'srt': Legendas SubRip com timestamps [hh:mm:ss,ms]
    - 'vtt': WebVTT para navegadores e players web
    - 'txt': Texto transcrito com timestamps ou limpo
    """
    fmt = output_format.lower().strip().lstrip(".")
    segments = sync_data.get("segments") or sync_data.get("sentences") or []
    words = sync_data.get("words") or []
    audio_name = sync_data.get("audio", "audiobook")

    use_items = words if (granularity == "word" and words) else segments
    if not use_items and words:
        use_items = words

    formatted_output = ""

    if fmt in ("json", "sync.json"):
        formatted_output = json.dumps(sync_data, ensure_ascii=False, indent=2)

    elif fmt == "lrc":
        lines = [
            f"[ar:{audio_name}]",
            f"[ti:{audio_name}]",
            "[re:Audiobook-Text Converter]",
            "",
        ]
        for it in use_items:
            t_str = format_time_lrc(it["start"])
            text_str = it.get("text", "").strip()
            if text_str:
                lines.append(f"{t_str} {text_str}")
        formatted_output = "\n".join(lines)

    elif fmt == "srt":
        blocks = []
        for idx, it in enumerate(use_items, 1):
            t_start = format_time_srt(it["start"])
            t_end = format_time_srt(it["end"])
            text_str = it.get("text", "").strip()
            if text_str:
                blocks.append(f"{idx}\n{t_start} --> {t_end}\n{text_str}\n")
        formatted_output = "\n".join(blocks)

    elif fmt == "vtt":
        lines = ["WEBVTT - Audiobook Player Sync", "", "NOTE", f"Audio: {audio_name}", ""]
        for idx, it in enumerate(use_items, 1):
            t_start = format_time_vtt(it["start"])
            t_end = format_time_vtt(it["end"])
            text_str = it.get("text", "").strip()
            if text_str:
                lines.append(f"{idx}\n{t_start} --> {t_end}\n{text_str}\n")
        formatted_output = "\n".join(lines)

    elif fmt == "txt":
        lines = []
        for it in use_items:
            t_start = format_time_vtt(it["start"])
            t_end = format_time_vtt(it["end"])
            text_str = it.get("text", "").strip()
            if text_str:
                lines.append(f"[{t_start} - {t_end}] {text_str}")
        formatted_output = "\n".join(lines)

    else:
        raise ValueError(
            f"Formato de exportação '{fmt}' não suportado. Escolha entre: json, lrc, srt, vtt, txt."
        )

    if output_path:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(formatted_output, encoding="utf-8")

    return formatted_output


def split_into_sentences(text: str, ai_mode: bool = True) -> List[str]:
    """
    Divide um texto limpo em frases, preservando abreviações comuns
    (como 'Sr.', 'Dra.', 'etc.') para evitar cortes incorretos.
    """
    cleaned = clean_extracted_text(text, ai_mode=ai_mode)
    if not cleaned:
        return []

    # Protege abreviações conhecidas substituindo temporariamente o ponto final
    protected_placeholder = "\u0000"

    def _protect_abbrev(match: re.Match) -> str:
        word = match.group(1)
        if word.lower() in COMMON_ABBREVIATIONS:
            return f"{word}{protected_placeholder}"
        return match.group(0)

    protected_text = re.sub(r"\b([A-Za-zÀ-ÿ]{1,5})\.", _protect_abbrev, cleaned)

    raw_parts = _SENTENCE_SPLIT_REGEX.split(protected_text)
    sentences: List[str] = []
    for part in raw_parts:
        restored = part.replace(protected_placeholder, ".").strip()
        if restored:
            sentences.append(restored)

    return sentences


def split_into_words(text: str) -> List[str]:
    """
    Divide um texto (ou uma frase) em palavras mantendo a pontuação colada
    à palavra (ex.: 'cidade.' permanece 'cidade.', como no exemplo da especificação).
    """
    cleaned = clean_extracted_text(text)
    if not cleaned:
        return []
    return _WORD_TOKEN_REGEX.findall(cleaned)


def _word_acoustic_weight(word: str) -> float:
    """
    Calcula um peso relativo para estimar a duração da pronúncia de uma palavra.
    Considera o número de caracteres e adiciona pequenas pausas em pontuações.
    """
    alpha_len = sum(1 for ch in word if ch.isalnum())
    base_weight = max(1.0, alpha_len * 0.85 + 1.2)
    if word.endswith((".", "!", "?", "…")):
        base_weight += 2.5
    elif word.endswith((",", ";", ":", "—")):
        base_weight += 1.2
    return base_weight


PT_NUM_WORDS = {
    "zero": 0, "um": 1, "primeiro": 1, "dois": 2, "segundo": 2, "tres": 3, "três": 3, "terceiro": 3,
    "quatro": 4, "quarto": 4, "cinco": 5, "quinto": 5, "seis": 6, "sexto": 6, "sete": 7, "setimo": 7,
    "sétimo": 7, "oito": 8, "oitavo": 8, "nove": 9, "nono": 9, "dez": 10, "decimo": 10, "décimo": 10,
}

ROMAN_MAP = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}

CHAP_0_REGEX = re.compile(
    r"^\s*(?:0\s*[\-:\.\u2013\u2014]\s*|cap[ií]tulo\s*0\b\s*[\-:\.\u2013\u2014]?\s*)(.*)",
    re.IGNORECASE,
)

CHAP_1_REGEX = re.compile(
    r"^\s*(?:cap[ií]tulo|chapter|cap\.?)\s*(?:1\b|01\b|i\b|um\b|one\b|primeiro\b)(?:\s*[:\-\u2013\u2014\.]\s*|\s+|$)(.*)",
    re.IGNORECASE,
)

CHAP_ANY_REGEX = re.compile(
    r"^\s*(?:cap[ií]tulo|chapter|cap\.?)\s*([0-9]+|[ivxlcdm]+|[a-záàâãéêíóôõúç]+)\b(?:\s*[:\-\u2013\u2014\.]\s*|\s+|$)(.*)",
    re.IGNORECASE,
)

NUMBERED_HEADER_REGEX = re.compile(
    r"^\s*([0-9]+|[ivxlcdm]+)\s*[\.\-\u2013\u2014]\s+([A-Za-zÀ-ÿ0-9].*)$",
    re.IGNORECASE,
)

INTRO_REGEX = re.compile(
    r"^\s*(pref[aá]cio|pr[oó]logo|introdu[cç][aã]o|apresenta[cç][aã]o|dedicat[oó]ria|nota\s+(?:do\s+autor|editorial|pr[eé]via)|advert[eê]ncia|agradecimentos|proleg[oô]menos)\b(?:\s*[:\-\u2013\u2014\.]\s*|\s+|$)(.*)",
    re.IGNORECASE,
)


def parse_chapter_number(token: str) -> Optional[int]:
    """Converte números ordinais, cardinais ou algarismos romanos para inteiro."""
    s = token.strip().lower()
    if s.isdigit():
        return int(s)
    if s in PT_NUM_WORDS:
        return PT_NUM_WORDS[s]
    if s in ROMAN_MAP:
        return ROMAN_MAP[s]
    return None


def detect_chapter_header(line: str) -> Optional[Tuple[int, str]]:
    """
    Detecta se uma linha representa um título de capítulo ou seção introdutória.
    Retorna uma tupla (numero_do_capitulo, titulo_formatado) ou None.
    """
    line_clean = line.strip()
    if not line_clean:
        return None

    # Capítulo 0 explícito
    m0 = CHAP_0_REGEX.match(line_clean)
    if m0:
        sub = m0.group(1).strip().lstrip(":-–— ")
        title = f"0 - Introdução: {sub}" if sub and not sub.lower().startswith("introdu") else "0 - Introdução"
        return 0, title

    # Capítulo 1 explícito
    m1 = CHAP_1_REGEX.match(line_clean)
    if m1:
        rest = m1.group(1).strip().lstrip(":-–— ")
        title = f"Capítulo 1: {rest}" if rest else "Capítulo 1"
        return 1, title

    # Qualquer capítulo nomeado (Capítulo 2, Capítulo III, etc.)
    m_any = CHAP_ANY_REGEX.match(line_clean)
    if m_any:
        num = parse_chapter_number(m_any.group(1))
        if num is not None:
            rest = m_any.group(2).strip().lstrip(":-–— ")
            title = f"Capítulo {num}: {rest}" if rest else f"Capítulo {num}"
            return num, title

    # Numeração ordinal com título (ex.: '2. O Retorno', 'II - A Floresta')
    m_num = NUMBERED_HEADER_REGEX.match(line_clean)
    if m_num:
        num = parse_chapter_number(m_num.group(1))
        if num is not None:
            rest = m_num.group(2).strip().lstrip(":-–— ")
            title = f"Capítulo {num}: {rest}" if rest else f"Capítulo {num}"
            return num, title

    return None


@dataclass
class ChapterSection:
    """Representa um capítulo estruturado com seus trechos sincronizados."""
    index: int
    title: str
    items: List[TimedItem] = field(default_factory=list)


def structure_text_into_chapters(
    raw_text_or_paragraphs: Union[str, List[str]],
    ai_mode: bool = True,
) -> List[ChapterSection]:
    """
    Estrutura o texto do livro em capítulos padronizados para audiolivro:
    - O narrador de IA inicia anunciando o nome/número do capítulo, faz uma pausa
      acústica e em seguida inicia a leitura do texto.
    - Qualquer conteúdo que antecede o Capítulo 1 (dedicatória, prefácio, prólogo,
      apresentação, notas) é automaticamente estruturado e narrado como Capítulo 0: '0 - Introdução'.
    """
    if isinstance(raw_text_or_paragraphs, str):
        paragraphs = [
            p.strip()
            for p in re.split(
                r"\r?\n\s*\r?\n|\r?\n(?=Cap[ií]tulo|Chapter|Pref[aá]cio|Pr[oó]logo|0\s*[\-\.])",
                raw_text_or_paragraphs,
                flags=re.IGNORECASE,
            )
            if p.strip()
        ]
    else:
        paragraphs = [str(p).strip() for p in raw_text_or_paragraphs if str(p).strip()]

    if not paragraphs:
        return []

    # 1. Localiza se existe e em qual parágrafo começa o Capítulo 1
    chap_1_p_idx = None
    for idx, p in enumerate(paragraphs):
        first_line = p.split("\n")[0].strip()
        ch_info = detect_chapter_header(first_line)
        if ch_info and ch_info[0] == 1:
            chap_1_p_idx = idx
            break

    sections: List[ChapterSection] = []

    # 2. Caso haja conteúdo anterior ao Capítulo 1 -> Capítulo 0 ('0 - Introdução')
    if chap_1_p_idx is not None and chap_1_p_idx > 0:
        pre_paragraphs = paragraphs[:chap_1_p_idx]
        first_p = pre_paragraphs[0].split("\n")[0].strip()
        m0 = CHAP_0_REGEX.match(first_p)
        mi = INTRO_REGEX.match(first_p)

        if m0:
            sub = m0.group(1).strip().lstrip(":-–— ")
            title = f"0 - Introdução: {sub}" if sub and not sub.lower().startswith("introdu") else "0 - Introdução"
            start_idx = 1
        elif mi:
            sec_name = mi.group(1).strip().capitalize()
            rest = mi.group(2).strip().lstrip(":-–— ")
            if not rest or len(rest) <= 30:
                title = f"0 - Introdução: {sec_name}" if not sec_name.lower().startswith("introdu") else "0 - Introdução"
                start_idx = 1
            else:
                title = "0 - Introdução"
                start_idx = 0
        else:
            title = "0 - Introdução"
            start_idx = 0

        heading_spoken = title if title.endswith(".") else f"{title}."
        ch0_items = [
            TimedItem(start=0.0, end=0.0, text=heading_spoken, chapter=0, is_chapter_title=True)
        ]

        for p in pre_paragraphs[start_idx:]:
            cleaned_p = clean_extracted_text(p, ai_mode=ai_mode)
            if not cleaned_p:
                continue
            for s in split_into_sentences(cleaned_p, ai_mode=ai_mode):
                ch0_items.append(TimedItem(start=0.0, end=0.0, text=s, chapter=0, is_chapter_title=False))

        sections.append(ChapterSection(index=0, title=title, items=ch0_items))
        remaining_paragraphs = paragraphs[chap_1_p_idx:]

    elif chap_1_p_idx is None:
        # Texto não contém Capítulo 1 explícito
        first_p = paragraphs[0].split("\n")[0].strip()
        m0 = CHAP_0_REGEX.match(first_p)
        mi = INTRO_REGEX.match(first_p)

        if m0 or mi:
            if m0:
                sub = m0.group(1).strip().lstrip(":-–— ")
                title = f"0 - Introdução: {sub}" if sub and not sub.lower().startswith("introdu") else "0 - Introdução"
            else:
                sec_name = mi.group(1).strip().capitalize()
                rest = mi.group(2).strip().lstrip(":-–— ")
                title = f"0 - Introdução: {sec_name}" if not sec_name.lower().startswith("introdu") else "0 - Introdução"

            heading_spoken = title if title.endswith(".") else f"{title}."
            ch0_items = [
                TimedItem(start=0.0, end=0.0, text=heading_spoken, chapter=0, is_chapter_title=True)
            ]
            for p in paragraphs[1:]:
                cleaned_p = clean_extracted_text(p, ai_mode=ai_mode)
                if not cleaned_p:
                    continue
                for s in split_into_sentences(cleaned_p, ai_mode=ai_mode):
                    ch0_items.append(TimedItem(start=0.0, end=0.0, text=s, chapter=0, is_chapter_title=False))
            return [ChapterSection(index=0, title=title, items=ch0_items)]
        else:
            title = "Capítulo 1"
            heading_spoken = "Capítulo 1."
            ch1_items = [
                TimedItem(start=0.0, end=0.0, text=heading_spoken, chapter=1, is_chapter_title=True)
            ]
            for p in paragraphs:
                cleaned_p = clean_extracted_text(p, ai_mode=ai_mode)
                if not cleaned_p:
                    continue
                for s in split_into_sentences(cleaned_p, ai_mode=ai_mode):
                    ch1_items.append(TimedItem(start=0.0, end=0.0, text=s, chapter=1, is_chapter_title=False))
            return [ChapterSection(index=1, title=title, items=ch1_items)]
    else:
        # Começa diretamente no Capítulo 1
        remaining_paragraphs = paragraphs

    # 3. Processa a partir do Capítulo 1 em diante
    current_sec: Optional[ChapterSection] = None
    for p in remaining_paragraphs:
        lines = [l.strip() for l in p.split("\n") if l.strip()]
        if not lines:
            continue
        first_line = lines[0]
        ch_info = detect_chapter_header(first_line)

        if ch_info:
            num, title = ch_info
            heading_spoken = title if title.endswith(".") else f"{title}."
            current_sec = ChapterSection(index=num, title=title)
            current_sec.items.append(
                TimedItem(start=0.0, end=0.0, text=heading_spoken, chapter=num, is_chapter_title=True)
            )
            sections.append(current_sec)

            body_lines = lines[1:]
            if body_lines:
                body_text = clean_extracted_text(" ".join(body_lines), ai_mode=ai_mode)
                for s in split_into_sentences(body_text, ai_mode=ai_mode):
                    current_sec.items.append(
                        TimedItem(start=0.0, end=0.0, text=s, chapter=num, is_chapter_title=False)
                    )
        else:
            if current_sec is None:
                current_sec = ChapterSection(index=1, title="Capítulo 1")
                heading_spoken = "Capítulo 1."
                current_sec.items.append(
                    TimedItem(start=0.0, end=0.0, text=heading_spoken, chapter=1, is_chapter_title=True)
                )
                sections.append(current_sec)

            cleaned_p = clean_extracted_text(p, ai_mode=ai_mode)
            for s in split_into_sentences(cleaned_p, ai_mode=ai_mode):
                current_sec.items.append(
                    TimedItem(start=0.0, end=0.0, text=s, chapter=current_sec.index, is_chapter_title=False)
                )

    return sections


def estimate_sentence_timings(
    sentences: Union[List[str], List[TimedItem]],
    total_duration: Optional[float] = None,
    words_per_minute: float = 120.0,
) -> List[TimedItem]:
    """
    Atribui tempos [start, end] às frases proporcionalmente ao peso acústico,
    incorporando pausas naturais de respiração, pontuação e uma pausa estendida (1.75s)
    após o anúncio de cada capítulo para permitir que o ouvinte processe o título.
    """
    if not sentences:
        return []

    item_list: List[TimedItem] = []
    for s in sentences:
        if isinstance(s, TimedItem):
            item_list.append(s)
        else:
            item_list.append(TimedItem(start=0.0, end=0.0, text=str(s)))

    sentence_weights: List[float] = []
    sentence_pauses: List[float] = []
    total_words = 0

    for item in item_list:
        sent = item.text
        words = split_into_words(sent)
        total_words += len(words)
        weight = sum(_word_acoustic_weight(w) for w in words) if words else 1.0
        sentence_weights.append(weight)

        # Pausas acústicas:
        if item.is_chapter_title:
            # Pausa estendida após narração do nome do capítulo antes de iniciar o texto
            p = 1.75
        else:
            s_strip = sent.strip()
            if s_strip.endswith((".", "!", "?", "…")):
                p = 0.75
            elif s_strip.endswith((":", ";", "—", "-")):
                p = 0.45
            else:
                p = 0.30
        sentence_pauses.append(p)

    sum_weights = sum(sentence_weights) or 1.0
    sum_pauses = sum(sentence_pauses[:-1]) if len(sentence_pauses) > 1 else 0.0

    if total_duration is None or total_duration <= 0:
        # Velocidade realista de audiolivro em português + pausas
        tempo_fala = (total_words / max(30.0, words_per_minute)) * 60.0
        total_duration = max(1.0, tempo_fala + sum_pauses)

    # Duração líquida dedicada à fala das palavras
    if total_duration > sum_pauses + len(item_list) * 0.4:
        available_speech_duration = total_duration - sum_pauses
    else:
        fator = max(0.1, (total_duration * 0.25) / max(0.1, sum_pauses))
        sentence_pauses = [p * fator for p in sentence_pauses]
        sum_pauses = sum(sentence_pauses[:-1]) if len(sentence_pauses) > 1 else 0.0
        available_speech_duration = max(0.5, total_duration - sum_pauses)

    items: List[TimedItem] = []
    cursor = 0.0
    num_s = len(item_list)

    for idx, (orig_item, weight, pause) in enumerate(
        zip(item_list, sentence_weights, sentence_pauses)
    ):
        dur_fala = (weight / sum_weights) * available_speech_duration
        end_time = cursor + dur_fala
        if idx == num_s - 1 and total_duration:
            end_time = max(end_time, total_duration)

        items.append(
            TimedItem(
                start=round(cursor, 2),
                end=round(end_time, 2),
                text=orig_item.text,
                audio_src=orig_item.audio_src,
                chapter=orig_item.chapter,
                is_chapter_title=orig_item.is_chapter_title,
            )
        )
        cursor = end_time + pause

    return items


def subdivide_segments_into_words(segments: List[TimedItem]) -> List[TimedItem]:
    """
    Converte segmentos de frases já temporizados em palavras individuais temporizadas,
    introduzindo micropausas naturais entre palavras e preservando metadados de capítulo.
    """
    word_items: List[TimedItem] = []
    for seg in segments:
        words = split_into_words(seg.text)
        if not words:
            continue
        seg_duration = max(0.05, seg.end - seg.start)
        weights = [_word_acoustic_weight(w) for w in words]

        # Micropausas entre palavras (0.20s após vírgulas e 0.04s entre palavras normais)
        pauses = []
        for w in words[:-1]:
            if w.endswith((",", ";", ":", "—")):
                pauses.append(0.20)
            else:
                pauses.append(0.04)
        sum_p = sum(pauses)

        # Garante que as pausas não consumam mais de 30% do tempo da frase
        if sum_p > seg_duration * 0.30:
            scale = (seg_duration * 0.30) / max(0.01, sum_p)
            pauses = [p * scale for p in pauses]
            sum_p = sum(pauses)

        available_word_duration = max(len(words) * 0.04, seg_duration - sum_p)
        total_w = sum(weights) or 1.0

        cursor = seg.start
        for idx, (word, w) in enumerate(zip(words, weights)):
            w_dur = (w / total_w) * available_word_duration
            w_end = cursor + w_dur
            if idx == len(words) - 1:
                w_end = seg.end

            word_items.append(
                TimedItem(
                    start=round(cursor, 2),
                    end=round(w_end, 2),
                    text=word,
                    audio_src=seg.audio_src,
                    chapter=seg.chapter,
                    is_chapter_title=seg.is_chapter_title,
                )
            )
            pause_after = pauses[idx] if idx < len(pauses) else 0.0
            cursor = w_end + pause_after

    return word_items


def group_words_into_sentences(words: List[TimedItem]) -> List[TimedItem]:
    """
    Agrupa palavras temporizadas em frases completas, preservando o `start` da
    primeira palavra, o `end` da última palavra e os metadados de capítulo.
    """
    if not words:
        return []

    segments: List[TimedItem] = []
    current_words: List[str] = []
    seg_start: float = words[0].start
    seg_end: float = words[0].end

    for idx, item in enumerate(words):
        if not current_words:
            seg_start = item.start
        current_words.append(item.text)
        seg_end = item.end

        # Verifica se a palavra encerra uma frase (e não é uma abreviação conhecida)
        stripped = item.text.rstrip('”"\'»)')
        clean_token = stripped.rstrip(".!?…").lower()
        ends_with_terminal = stripped.endswith((".", "!", "?", "…"))
        is_abbrev = stripped.endswith(".") and clean_token in COMMON_ABBREVIATIONS

        # Também fecha se houver mudança de capítulo na próxima palavra
        next_is_diff_chapter = (
            idx + 1 < len(words) and words[idx + 1].chapter != item.chapter
        )

        if (ends_with_terminal and not is_abbrev) or next_is_diff_chapter or idx == len(words) - 1:
            segments.append(
                TimedItem(
                    start=seg_start,
                    end=seg_end,
                    text=" ".join(current_words),
                    audio_src=item.audio_src,
                    chapter=item.chapter,
                    is_chapter_title=item.is_chapter_title,
                )
            )
            current_words = []

    return segments


# =============================================================================
# 3. PARSER DE EPUB 3 MEDIA OVERLAYS (SMIL) E EPUB COMUM
# =============================================================================

def parse_smil_clock_value(clock_str: Optional[str]) -> float:
    """
    Converte valores de tempo da especificação SMIL 3.0 / EPUB Media Overlays para segundos.
    Formatos suportados:
    - '3.2s' -> 3.2
    - '500ms' -> 0.5
    - '2.5min' -> 150.0
    - '1.5h' -> 5400.0
    - '01:02:03.5' (hh:mm:ss.fraction) -> 3723.5
    - '02:15.4' (mm:ss.fraction) -> 135.4
    - '12.4' -> 12.4
    """
    if not clock_str:
        return 0.0
    val = clock_str.strip().lower()
    if not val:
        return 0.0

    try:
        if val.endswith("ms"):
            return float(val[:-2].strip()) / 1000.0
        if val.endswith("min"):
            return float(val[:-3].strip()) * 60.0
        if val.endswith("h"):
            return float(val[:-1].strip()) * 3600.0
        if val.endswith("s"):
            return float(val[:-1].strip())
        if ":" in val:
            parts = [float(p) for p in val.split(":")]
            if len(parts) == 3:
                return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60.0 + parts[1]
        return float(val)
    except ValueError as exc:
        raise ValueError(f"Formato de tempo SMIL inválido: '{clock_str}'") from exc


def _strip_html_tags(xhtml_bytes_or_str: Union[bytes, str]) -> str:
    """Extrai o texto de um documento HTML/XHTML preservando limites de parágrafo."""
    if isinstance(xhtml_bytes_or_str, bytes):
        text_data = xhtml_bytes_or_str.decode("utf-8", errors="replace")
    else:
        text_data = xhtml_bytes_or_str

    if HAS_BS4:
        soup = BeautifulSoup(text_data, "html.parser")
        for tag in soup(["script", "style", "head", "nav"]):
            tag.decompose()
        return soup.get_text(separator="\n\n")

    # Fallback puro com regex se BeautifulSoup não estiver disponível
    no_scripts = re.sub(
        r"<(script|style|head|nav)[^>]*>.*?</\1>", "", text_data, flags=re.DOTALL | re.I
    )
    no_tags = re.sub(r"<[^>]+>", "\n\n", no_scripts)
    return no_tags


def _extract_xhtml_fragments_by_id(xhtml_content: Union[bytes, str]) -> Dict[str, str]:
    """
    Mapeia todos os elementos com atributo `id` ou `xml:id` dentro de um XHTML
    para seu respectivo texto limpo (usado para resolver `chapter1.xhtml#sentence1` do SMIL).
    """
    if isinstance(xhtml_content, bytes):
        text_data = xhtml_content.decode("utf-8", errors="replace")
    else:
        text_data = xhtml_content

    fragments: Dict[str, str] = {}

    if HAS_BS4:
        soup = BeautifulSoup(text_data, "html.parser")
        for elem in soup.find_all(attrs={"id": True}):
            frag_id = elem.get("id")
            if frag_id:
                fragments[str(frag_id)] = clean_extracted_text(elem.get_text(separator=" "))
        for elem in soup.find_all(attrs={"xml:id": True}):
            frag_id = elem.get("xml:id")
            if frag_id and str(frag_id) not in fragments:
                fragments[str(frag_id)] = clean_extracted_text(elem.get_text(separator=" "))
        return fragments

    # Fallback com ElementTree
    try:
        root = ET.fromstring(text_data)
        for elem in root.iter():
            frag_id = elem.attrib.get("id") or elem.attrib.get(
                "{http://www.w3.org/XML/1998/namespace}id"
            )
            if frag_id:
                inner_text = "".join(elem.itertext())
                fragments[frag_id] = clean_extracted_text(inner_text)
    except ET.ParseError:
        pass

    return fragments


def extract_from_epub(epub_path: Union[str, Path]) -> Tuple[List[TimedItem], bool, Dict[str, Any]]:
    """
    Extrai conteúdo de um arquivo EPUB.
    - Se o EPUB contiver Media Overlays (arquivos .smil), resolve as referências
      entre o SMIL e os elementos XHTML (`#id`) e retorna os segmentos já com os
      tempos reais (`has_media_overlays = True`).
    - Se for um EPUB comum sem SMIL, extrai o texto dos capítulos na ordem de
      leitura (`spine`) e retorna itens com `start=0, end=0` (`has_media_overlays = False`).
    """
    epub_path = Path(epub_path)
    metadata: Dict[str, Any] = {"title": None, "author": None, "chapters": []}

    with zipfile.ZipFile(epub_path, "r") as zf:
        namelist = set(zf.namelist())

        # 1. Localiza o arquivo OPF via META-INF/container.xml
        opf_path: Optional[str] = None
        if "META-INF/container.xml" in namelist:
            container_xml = zf.read("META-INF/container.xml")
            container_root = ET.fromstring(container_xml)
            for elem in container_root.iter():
                if elem.tag.endswith("rootfile") and "full-path" in elem.attrib:
                    opf_path = elem.attrib["full-path"]
                    break

        if not opf_path:
            # Procura qualquer arquivo .opf no pacote
            opf_candidates = [n for n in namelist if n.lower().endswith(".opf")]
            opf_path = opf_candidates[0] if opf_candidates else None

        opf_dir = posixpath.dirname(opf_path) if opf_path else ""
        manifest_items: Dict[str, Dict[str, str]] = {}
        spine_ids: List[str] = []
        smil_files_ordered: List[str] = []

        if opf_path and opf_path in namelist:
            opf_root = ET.fromstring(zf.read(opf_path))

            # Extrai metadados básicos (dc:title, dc:creator)
            for elem in opf_root.iter():
                tag_local = elem.tag.split("}")[-1].lower()
                if tag_local == "title" and elem.text and not metadata["title"]:
                    metadata["title"] = elem.text.strip()
                elif tag_local == "creator" and elem.text and not metadata["author"]:
                    metadata["author"] = elem.text.strip()
                elif tag_local == "item":
                    item_id = elem.attrib.get("id", "")
                    href = elem.attrib.get("href", "")
                    media_type = elem.attrib.get("media-type", "")
                    media_overlay = elem.attrib.get("media-overlay", "")
                    full_href = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
                    manifest_items[item_id] = {
                        "href": full_href,
                        "media_type": media_type,
                        "media_overlay": media_overlay,
                    }
                elif tag_local == "itemref":
                    idref = elem.attrib.get("idref", "")
                    if idref:
                        spine_ids.append(idref)

            # Ordena arquivos SMIL conforme a ordem do spine (ou coleta todos os SMIL do manifesto)
            seen_smil: set[str] = set()
            for idref in spine_ids:
                item = manifest_items.get(idref)
                if not item:
                    continue
                overlay_id = item.get("media_overlay")
                if overlay_id and overlay_id in manifest_items:
                    smil_href = manifest_items[overlay_id]["href"]
                    if smil_href in namelist and smil_href not in seen_smil:
                        smil_files_ordered.append(smil_href)
                        seen_smil.add(smil_href)

            for item in manifest_items.values():
                if (
                    item["media_type"] == "application/smil+xml"
                    or item["href"].lower().endswith(".smil")
                ):
                    if item["href"] in namelist and item["href"] not in seen_smil:
                        smil_files_ordered.append(item["href"])
                        seen_smil.add(item["href"])

        # Fallback caso o EPUB tenha arquivos .smil fora do OPF
        if not smil_files_ordered:
            smil_files_ordered = sorted(
                n for n in namelist if n.lower().endswith(".smil")
            )

        # 2. Se houver Media Overlays (SMIL), resolve <par> (<text> + <audio>)
        if smil_files_ordered:
            xhtml_cache: Dict[str, Dict[str, str]] = {}
            timed_segments: List[TimedItem] = []

            # Mantém deslocamento acumulado por arquivo de áudio caso o livro tenha múltiplos MP3
            audio_file_offsets: Dict[str, float] = {}
            audio_file_max_end: Dict[str, float] = {}
            cumulative_offset = 0.0
            last_audio_src: Optional[str] = None

            for smil_path in smil_files_ordered:
                smil_dir = posixpath.dirname(smil_path)
                smil_root = ET.fromstring(zf.read(smil_path))

                for par in smil_root.iter():
                    if not par.tag.split("}")[-1].lower() == "par":
                        continue

                    text_src: Optional[str] = None
                    audio_src: Optional[str] = None
                    clip_begin: Optional[str] = None
                    clip_end: Optional[str] = None

                    for child in par:
                        c_tag = child.tag.split("}")[-1].lower()
                        if c_tag == "text":
                            text_src = child.attrib.get("src")
                        elif c_tag == "audio":
                            audio_src = child.attrib.get("src")
                            clip_begin = child.attrib.get("clipBegin")
                            clip_end = child.attrib.get("clipEnd")

                    if not text_src or clip_begin is None or clip_end is None:
                        continue

                    # Separa caminho do XHTML e identificador do fragmento (#sentence1)
                    if "#" in text_src:
                        rel_xhtml, frag_id = text_src.split("#", 1)
                    else:
                        rel_xhtml, frag_id = text_src, ""

                    xhtml_path = (
                        posixpath.normpath(posixpath.join(smil_dir, rel_xhtml))
                        if smil_dir
                        else posixpath.normpath(rel_xhtml)
                    )

                    if xhtml_path not in xhtml_cache:
                        if xhtml_path in namelist:
                            xhtml_cache[xhtml_path] = _extract_xhtml_fragments_by_id(
                                zf.read(xhtml_path)
                            )
                        else:
                            xhtml_cache[xhtml_path] = {}

                    resolved_text = xhtml_cache[xhtml_path].get(frag_id, "")
                    if not resolved_text and xhtml_path in namelist and not frag_id:
                        resolved_text = _strip_html_tags(zf.read(xhtml_path))

                    if not resolved_text:
                        continue

                    raw_start = parse_smil_clock_value(clip_begin)
                    raw_end = parse_smil_clock_value(clip_end)

                    norm_audio = (
                        posixpath.normpath(posixpath.join(smil_dir, audio_src))
                        if (audio_src and smil_dir)
                        else (audio_src or "default_audio")
                    )

                    if last_audio_src is not None and norm_audio != last_audio_src:
                        if norm_audio not in audio_file_offsets:
                            cumulative_offset = max(audio_file_max_end.values(), default=0.0)
                            audio_file_offsets[norm_audio] = cumulative_offset
                    elif norm_audio not in audio_file_offsets:
                        audio_file_offsets[norm_audio] = 0.0

                    offset = audio_file_offsets[norm_audio]
                    adj_start = offset + raw_start
                    adj_end = offset + raw_end
                    audio_file_max_end[norm_audio] = max(
                        audio_file_max_end.get(norm_audio, 0.0), adj_end
                    )
                    last_audio_src = norm_audio

                    timed_segments.append(
                        TimedItem(
                            start=adj_start,
                            end=adj_end,
                            text=resolved_text,
                            audio_src=audio_src,
                        )
                    )

            if timed_segments:
                return timed_segments, True, metadata

        # 3. EPUB comum sem Media Overlays: extrai o texto na ordem do spine
        xhtml_files: List[str] = []
        for idref in spine_ids:
            item = manifest_items.get(idref)
            if item and item["href"] in namelist:
                xhtml_files.append(item["href"])

        if not xhtml_files:
            xhtml_files = sorted(
                n
                for n in namelist
                if n.lower().endswith((".xhtml", ".html", ".htm"))
                and not posixpath.basename(n).lower().startswith("nav")
            )

        all_paragraphs: List[TimedItem] = []
        for xhtml_file in xhtml_files:
            chapter_text = _strip_html_tags(zf.read(xhtml_file))
            if not chapter_text:
                continue
            metadata["chapters"].append({"file": xhtml_file, "char_length": len(chapter_text)})
            for p in chapter_text.split("\n\n"):
                p_str = p.strip()
                if p_str:
                    all_paragraphs.append(TimedItem(start=0.0, end=0.0, text=p_str))

        return all_paragraphs, False, metadata


# =============================================================================
# 4. EXTRATORES PARA MOBI, PDF, TXT E FORMATOS DE LEGENDA (LRC / WEBVTT / SRT)
# =============================================================================

def _decompress_palmdoc_lz77(data: bytes) -> bytes:
    """
    Descompacta um registro binário comprimido com o algoritmo PalmDOC LZ77
    (padrão utilizado em arquivos de ebook .mobi / .prc / .azw).
    """
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        i += 1
        if b == 0x00:
            out.append(0x00)
        elif 0x01 <= b <= 0x08:
            # Copia os próximos 1..8 bytes literalmente
            out.extend(data[i : i + b])
            i += b
        elif 0x09 <= b <= 0x7F:
            # Caractere ASCII literal
            out.append(b)
        elif 0x80 <= b <= 0xBF:
            # Par distância-comprimento (2 bytes)
            if i >= n:
                break
            b2 = data[i]
            i += 1
            combined = ((b << 8) | b2) & 0x3FFF
            distance = combined >> 3
            length = (combined & 0x07) + 3
            if distance == 0 or distance > len(out):
                continue
            for _ in range(length):
                out.append(out[-distance])
        else:
            # 0xC0..0xFF: espaço seguido de caractere ASCII (b ^ 0x80)
            out.append(0x20)
            out.append(b ^ 0x80)
    return bytes(out)


def extract_text_from_mobi(mobi_path: Union[str, Path]) -> str:
    """
    Extrai o texto de um arquivo .mobi / .azw / .prc.
    Implementa um leitor nativo de contêiner Palm Database (PDB) + PalmDOC LZ77,
    não exigindo bibliotecas externas para arquivos MOBI padrão.
    """
    raw = Path(mobi_path).read_bytes()
    if len(raw) < 78:
        raise ValueError("Arquivo MOBI inválido ou corrompido (cabeçalho menor que 78 bytes).")

    # Verifica assinatura PDB ou cabeçalho de registros
    num_records = struct.unpack(">H", raw[76:78])[0]
    if num_records < 2:
        raise ValueError("Arquivo MOBI sem registros de texto suficientes.")

    record_offsets: List[int] = []
    pos = 78
    for _ in range(num_records):
        if pos + 8 > len(raw):
            break
        offset = struct.unpack(">I", raw[pos : pos + 4])[0]
        record_offsets.append(offset)
        pos += 8
    record_offsets.append(len(raw))

    # Registro 0 contém o cabeçalho PalmDOC + cabeçalho MOBI
    rec0 = raw[record_offsets[0] : record_offsets[1]]
    if len(rec0) < 16:
        raise ValueError("Cabeçalho PalmDOC ausente no registro 0 do MOBI.")

    compression, _unused, _text_length, text_record_count, _record_size = struct.unpack(
        ">HHIHH", rec0[:12]
    )

    # Verifica codificação no cabeçalho MOBI (offset 28 do rec0 = text_encoding: 65001=UTF-8, 1252=CP1252)
    encoding = "utf-8"
    if len(rec0) >= 32 and rec0[16:20] == b"MOBI":
        codepage = struct.unpack(">I", rec0[28:32])[0]
        if codepage == 1252:
            encoding = "cp1252"

    # Verifica flags de extra bytes no final de cada registro (EXTH / trailing entries)
    extra_flags = 0
    if len(rec0) >= 0xF4 and rec0[16:20] == b"MOBI":
        header_len = struct.unpack(">I", rec0[20:24])[0]
        if header_len >= 0xE4 and len(rec0) >= 16 + 0xE4:
            extra_flags = struct.unpack(">H", rec0[16 + 0xE2 : 16 + 0xE4])[0]

    def _trim_trailing_entries(record: bytes, flags: int) -> bytes:
        if not flags or not record:
            return record
        # Remove entradas finais de tamanho variável conforme especificação MOBI
        temp_flags = flags >> 1
        while temp_flags:
            if temp_flags & 1:
                # Lê varint de trás para frente
                size = 0
                shift = 0
                idx = len(record) - 1
                while idx >= 0:
                    byte_val = record[idx]
                    size |= (byte_val & 0x7F) << shift
                    shift += 7
                    idx -= 1
                    if byte_val & 0x80:
                        break
                if 0 < size <= len(record):
                    record = record[:-size]
            temp_flags >>= 1
        if flags & 1 and record:
            multibyte_overlap = (record[-1] & 0x03) + 1
            if multibyte_overlap <= len(record):
                record = record[:-multibyte_overlap]
        return record

    max_rec = min(text_record_count, len(record_offsets) - 2)
    decompressed_chunks: List[bytes] = []

    for r_idx in range(1, max_rec + 1):
        chunk = raw[record_offsets[r_idx] : record_offsets[r_idx + 1]]
        chunk = _trim_trailing_entries(chunk, extra_flags)
        if compression == 1:
            # Sem compressão
            decompressed_chunks.append(chunk)
        elif compression == 2:
            # Compressão PalmDOC LZ77
            decompressed_chunks.append(_decompress_palmdoc_lz77(chunk))
        else:
            raise ValueError(
                f"Compressão MOBI HUFF/CDIC ({compression}) requer conversão externa."
            )

    full_html = b"".join(decompressed_chunks).decode(encoding, errors="replace")
    return _strip_html_tags(full_html)


def extract_text_from_pdf(pdf_path: Union[str, Path]) -> str:
    """Extrai texto de todas as páginas de um arquivo PDF preservando parágrafos."""
    pdf_path = Path(pdf_path)
    pages_text: List[str] = []

    if HAS_PYPDF:
        reader = pypdf.PdfReader(str(pdf_path))
        for page in reader.pages:
            page_txt = page.extract_text() or ""
            if page_txt.strip():
                pages_text.append(page_txt.strip())
    elif HAS_PDFPLUMBER:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                page_txt = page.extract_text() or ""
                if page_txt.strip():
                    pages_text.append(page_txt.strip())
    else:
        raise RuntimeError(
            "Nenhuma biblioteca de leitura de PDF (pypdf ou pdfplumber) está instalada."
        )

    return "\n\n".join(pages_text)


def extract_text_from_txt(txt_path: Union[str, Path]) -> str:
    """Lê arquivos .txt testando codificações comuns (UTF-8, UTF-8-SIG, CP1252, Latin-1) preservando parágrafos."""
    raw_bytes = Path(txt_path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def extract_from_lrc_or_vtt(
    sync_file_path: Union[str, Path], default_last_duration: float = 3.0
) -> List[TimedItem]:
    """
    Suporte adicional para importação de arquivos de legenda/sincronização (.lrc e .vtt/.srt).
    """
    content = Path(sync_file_path).read_text(encoding="utf-8", errors="replace")
    ext = Path(sync_file_path).suffix.lower()
    items: List[TimedItem] = []

    if ext == ".lrc":
        # Formato LRC: [mm:ss.xx]Texto da linha
        lrc_pattern = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
        raw_entries: List[Tuple[float, str]] = []
        for line in content.splitlines():
            m = lrc_pattern.match(line.strip())
            if m:
                minutes = int(m.group(1))
                seconds = float(m.group(2))
                text = clean_extracted_text(m.group(3))
                if text:
                    raw_entries.append((minutes * 60.0 + seconds, text))
        for idx, (start_t, text) in enumerate(raw_entries):
            end_t = (
                raw_entries[idx + 1][0]
                if idx + 1 < len(raw_entries)
                else start_t + default_last_duration
            )
            items.append(TimedItem(start=start_t, end=end_t, text=text))
        return items

    # WebVTT (.vtt) ou SubRip (.srt)
    cue_pattern = re.compile(
        r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d+:)?\d{2}:\d{2}[.,]\d{3})"
    )
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = cue_pattern.search(lines[i])
        if m:
            start_str = m.group(1).replace(",", ".")
            end_str = m.group(2).replace(",", ".")
            start_t = parse_smil_clock_value(start_str)
            end_t = parse_smil_clock_value(end_str)
            i += 1
            text_lines: List[str] = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            cue_text = _strip_html_tags(" ".join(text_lines))
            if cue_text:
                items.append(TimedItem(start=start_t, end=end_t, text=cue_text))
        else:
            i += 1

    return items


# =============================================================================
# 5. LEITURA DE METADADOS DO AUDIOBOOK (ID3 SYLT / CAPÍTULOS)
# =============================================================================

def inspect_audiobook_metadata(audio_path: Union[str, Path]) -> AudioMetadataResult:
    """
    Inspeciona um arquivo de áudio (ex.: .mp3, .m4b) em busca de:
    1. Metadados básicos (título, autor, capítulos, duração)
    2. Texto sincronizado embutido nos metadados (como a tag ID3v2 SYLT em MP3).

    Se encontrar apenas título, autor ou capítulos (sem SYLT ou faixa sincronizada),
    informa explicitamente que não encontrou texto sincronizado.
    """
    audio_path = Path(audio_path)
    result = AudioMetadataResult(audio_filename=audio_path.name)

    if not audio_path.exists():
        result.message = f"Arquivo de áudio '{audio_path.name}' não encontrado no disco."
        return result

    # 1. Usa mutagen se disponível no ambiente
    if HAS_MUTAGEN:
        try:
            audio_file = mutagen.File(str(audio_path))
            if audio_file is not None and getattr(audio_file, "info", None):
                result.duration = float(getattr(audio_file.info, "length", 0.0) or 0.0)
            if audio_file is not None and audio_file.tags:
                tags = audio_file.tags
                for key, val in tags.items():
                    if key.startswith("TIT2") or key == "\xa9nam":
                        result.title = str(val[0] if isinstance(val, list) else val)
                    elif key.startswith("TPE1") or key == "\xa9ART":
                        result.author = str(val[0] if isinstance(val, list) else val)
                    elif key.startswith("CHAP"):
                        result.chapters.append({"id": key})
                    elif key.startswith("SYLT"):
                        # Extrai letras/transcrição sincronizada do frame SYLT
                        sylt_entries = getattr(val, "text", [])
                        for idx, (text_piece, timestamp_ms) in enumerate(sylt_entries):
                            start_s = float(timestamp_ms) / 1000.0
                            next_s = (
                                float(sylt_entries[idx + 1][1]) / 1000.0
                                if idx + 1 < len(sylt_entries)
                                else start_s + 2.5
                            )
                            cleaned = clean_extracted_text(str(text_piece))
                            if cleaned:
                                result.segments.append(
                                    TimedItem(start=start_s, end=next_s, text=cleaned)
                                )
                        if result.segments:
                            result.has_synchronized_text = True
                            result.sync_source = "ID3v2_SYLT"
        except Exception:
            pass

    # 2. Parser binário nativo de tags ID3v2.3 / ID3v2.4 (funciona sem mutagen)
    if not result.has_synchronized_text:
        raw_head = audio_path.read_bytes()[:262144]  # Lê até 256 KB iniciais
        if raw_head[:3] == b"ID3" and len(raw_head) >= 10:
            version_major = raw_head[3]
            tag_size = (
                ((raw_head[6] & 0x7F) << 21)
                | ((raw_head[7] & 0x7F) << 14)
                | ((raw_head[8] & 0x7F) << 7)
                | (raw_head[9] & 0x7F)
            )
            pos = 10
            end_pos = min(len(raw_head), 10 + tag_size)
            while pos + 10 <= end_pos:
                frame_id = raw_head[pos : pos + 4].decode("latin-1", errors="ignore")
                if not frame_id.strip("\x00"):
                    break
                if version_major == 4:
                    f_size = (
                        ((raw_head[pos + 4] & 0x7F) << 21)
                        | ((raw_head[pos + 5] & 0x7F) << 14)
                        | ((raw_head[pos + 6] & 0x7F) << 7)
                        | (raw_head[pos + 7] & 0x7F)
                    )
                else:
                    f_size = struct.unpack(">I", raw_head[pos + 4 : pos + 8])[0]

                frame_data = raw_head[pos + 10 : pos + 10 + f_size]
                pos += 10 + f_size

                if frame_id in ("TIT2", "TPE1") and len(frame_data) > 1:
                    enc_byte = frame_data[0]
                    txt_enc = "utf-16" if enc_byte in (1, 2) else "utf-8" if enc_byte == 3 else "latin-1"
                    decoded = frame_data[1:].decode(txt_enc, errors="replace").strip("\x00 ")
                    if frame_id == "TIT2":
                        result.title = decoded
                    else:
                        result.author = decoded
                elif frame_id == "CHAP":
                    result.chapters.append({"id": f"chap_{len(result.chapters) + 1}"})
                elif frame_id == "SYLT" and len(frame_data) > 6:
                    # Estrutura SYLT: [encoding(1)][lang(3)][time_format(1)][content_type(1)][desc\0][text\0 + time(4)]...
                    enc_byte = frame_data[0]
                    time_format = frame_data[4]  # 2 = milissegundos
                    txt_enc = "utf-8" if enc_byte == 3 else "latin-1"
                    # Pula descrição terminada em \x00
                    desc_end = frame_data.find(b"\x00", 6)
                    if desc_end != -1:
                        cursor = desc_end + 1
                        raw_sylt: List[Tuple[float, str]] = []
                        while cursor < len(frame_data):
                            nul_idx = frame_data.find(b"\x00", cursor)
                            if nul_idx == -1 or nul_idx + 5 > len(frame_data):
                                break
                            piece = frame_data[cursor:nul_idx].decode(txt_enc, errors="replace").strip()
                            ts_val = struct.unpack(">I", frame_data[nul_idx + 1 : nul_idx + 5])[0]
                            ts_sec = float(ts_val) / 1000.0 if time_format == 2 else float(ts_val)
                            if piece:
                                raw_sylt.append((ts_sec, piece))
                            cursor = nul_idx + 5
                        for idx, (s_time, s_text) in enumerate(raw_sylt):
                            e_time = (
                                raw_sylt[idx + 1][0]
                                if idx + 1 < len(raw_sylt)
                                else s_time + 2.5
                            )
                            result.segments.append(TimedItem(start=s_time, end=e_time, text=s_text))
                        if result.segments:
                            result.has_synchronized_text = True
                            result.sync_source = "ID3v2_SYLT"

    if result.has_synchronized_text:
        result.message = (
            f"Texto sincronizado encontrado nos metadados ({result.sync_source}) "
            f"com {len(result.segments)} trechos."
        )
    else:
        found_meta = []
        if result.title:
            found_meta.append(f"título='{result.title}'")
        if result.author:
            found_meta.append(f"autor='{result.author}'")
        if result.chapters:
            found_meta.append(f"{len(result.chapters)} capítulo(s)")
        meta_summary = ", ".join(found_meta) if found_meta else "nenhum metadado textual"
        result.message = (
            f"Não foi encontrado texto sincronizado nos metadados do áudio "
            f"(encontrado apenas: {meta_summary}). Importe um arquivo de ebook ou sincronização separado."
        )

    return result


inspect_audio_file = inspect_audiobook_metadata


# =============================================================================
# 6. VALIDAÇÃO DOS DADOS DE SINCRONIZAÇÃO
# =============================================================================

def validate_sync_data(
    sync_data: Dict[str, Any],
    audio_duration: Optional[float] = None,
    duration_tolerance: float = 2.0,
) -> ValidationReport:
    """
    Valida se o JSON de sincronização possui estrutura íntegra, tempos válidos
    (não negativos, `end > start`, ordem cronológica) e se corresponde à duração do áudio.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(sync_data, dict):
        return ValidationReport(is_valid=False, errors=["O objeto raiz deve ser um dicionário JSON."])

    if "audio" not in sync_data or not str(sync_data["audio"]).strip():
        errors.append("Campo obrigatório 'audio' ausente ou vazio.")

    has_segments = "segments" in sync_data and isinstance(sync_data["segments"], list)
    has_words = "words" in sync_data and isinstance(sync_data["words"], list)

    if not has_segments and not has_words:
        errors.append("O JSON deve conter a lista 'segments' (frases) e/ou 'words' (palavras).")
        return ValidationReport(is_valid=False, errors=errors)

    max_end = 0.0
    total_count = 0

    for list_key in ("segments", "words"):
        if list_key not in sync_data:
            continue
        items = sync_data[list_key]
        if not items:
            warnings.append(f"A lista '{list_key}' está vazia.")
            continue

        prev_end = 0.0
        for idx, item in enumerate(items):
            total_count += 1
            start = item.get("start")
            end = item.get("end")
            text = item.get("text", "")

            if start is None or end is None:
                errors.append(f"{list_key}[{idx}]: tempos 'start' ou 'end' ausentes.")
                continue
            if start < 0:
                errors.append(f"{list_key}[{idx}]: tempo inicial negativo ({start}s).")
            if end <= start:
                errors.append(
                    f"{list_key}[{idx}]: 'end' ({end}s) deve ser maior que 'start' ({start}s)."
                )
            if start < prev_end - 0.05:
                errors.append(
                    f"{list_key}[{idx}]: sobreposição temporal detectada "
                    f"(start={start}s < anterior end={prev_end}s)."
                )
            if not str(text).strip():
                warnings.append(f"{list_key}[{idx}]: texto vazio no intervalo [{start}, {end}].")

            prev_end = max(prev_end, float(end))
            max_end = max(max_end, float(end))

    if audio_duration is not None and audio_duration > 0 and max_end > 0:
        if max_end > audio_duration + duration_tolerance:
            errors.append(
                f"O tempo final do texto ({max_end:.2f}s) ultrapassa a duração do áudio "
                f"({audio_duration:.2f}s) além da tolerância ({duration_tolerance:.2f}s)."
            )
        elif max_end < audio_duration * 0.5:
            warnings.append(
                f"O tempo final do texto ({max_end:.2f}s) cobre menos de 50% da duração do áudio ({audio_duration:.2f}s)."
            )

    return ValidationReport(
        is_valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        total_items=total_count,
        max_end_time=round(max_end, 2),
    )


# =============================================================================
# 7. CONVERSOR PRINCIPAL (EBOOK / ÁUDIO -> JSON SINCRONIZADO)
# =============================================================================

class EbookAudiobookConverter:
    """
    Converte ebooks (.epub, .mobi, .pdf, .txt) ou arquivos de sincronização
    (.lrc, .vtt, .srt) para o formato JSON interno do player de audiobook,
    com destaque por frase ("segments"), por palavra ("words") ou ambos ("both").
    """

    SUPPORTED_EXTENSIONS = {
        ".epub", ".mobi", ".azw", ".prc", ".pdf", ".txt", ".lrc", ".vtt", ".srt", ".json"
    }

    def convert(
        self,
        ebook_path: Optional[Union[str, Path]] = None,
        audio_filename: str = "audiobook.mp3",
        granularity: GranularityMode = "both",
        audio_duration: Optional[float] = None,
        words_per_minute: float = 135.0,
        require_native_timestamps: bool = False,
        output_json_path: Optional[Union[str, Path]] = None,
        output_path: Optional[Union[str, Path]] = None,
        output_format: str = "json",
        include_metadata: bool = False,
        audio_path: Optional[Union[str, Path]] = None,
        ai_filter: bool = True,
        log_callback: Optional[Any] = None,
        custom_chapters: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Converte livros, textos ou arquivos de áudio para o formato desejado pelo usuário:
        1. Apenas Áudio -> Transcrição e temporização automática via IA (Whisper).
        2. Apenas Texto/Ebook -> Cadência ideal de leitura de IA com limpeza de ruídos (notas, hífens, páginas).
        3. Áudio + Texto -> Sincronização Dupla Real (voz do áudio + ortografia exata do livro).

        Formatos de saída suportados: 'json', 'lrc', 'srt', 'vtt', 'txt'.
        """
        destination_file = output_path or output_json_path
        resolved_format = output_format.lower().strip().lstrip(".")
        if destination_file:
            ext_file = Path(destination_file).suffix.lower().lstrip(".")
            if ext_file in ("json", "lrc", "srt", "vtt", "txt") and output_format == "json":
                resolved_format = ext_file

        has_native_timestamps = False
        sync_source = "estimated_ai_cadence"
        book_meta: Dict[str, Any] = {}
        base_segments: List[TimedItem] = []
        word_items: List[TimedItem] = []

        # Identifica se um arquivo de áudio real foi passado
        real_audio_file = None
        for candidate in (audio_path, audio_filename):
            if candidate:
                cand_path = Path(candidate)
                if cand_path.exists() and cand_path.is_file():
                    real_audio_file = cand_path
                    break

        if not ebook_path and not real_audio_file:
            raise ValueError(
                "Por favor, selecione ao menos um arquivo de texto/ebook ou um arquivo de áudio."
            )

        # ---------------------------------------------------------------------
        # ---------------------------------------------------------------------
        # MODO 1: APENAS ÁUDIO (sem arquivo de texto)
        # ---------------------------------------------------------------------
        if not ebook_path and real_audio_file:
            if log_callback:
                log_callback(
                    f"Modo Apenas Áudio: transcrevendo fala e calculando sincronia de '{real_audio_file.name}'..."
                )
                log_callback(
                    "[Aviso]: Usando só o áudio, o texto transcrito pela IA pode conter pequenas imprecisões em nomes próprios, homófonos ou pontuação."
                )
                log_callback(
                    "[Recomendação]: O ideal para o melhor resultado é usar AMBOS (Texto + Áudio) para garantir fidelidade 100% ao livro."
                )
            import sync_audiobook

            sync_data = sync_audiobook.transcribe_audio_to_sync(
                audio_path=real_audio_file,
                model_name="tiny",
                log_func=log_callback or print,
            )
            raw_segs = sync_data.get("segments") or []
            raw_words = sync_data.get("words") or []
            base_segments = [
                TimedItem(start=s["start"], end=s["end"], text=s["text"]) for s in raw_segs
            ]
            word_items = [
                TimedItem(start=w["start"], end=w["end"], text=w["text"]) for w in raw_words
            ]
            has_native_timestamps = True
            sync_source = "audio_transcription_whisper"

        # ---------------------------------------------------------------------
        # MODO 2 OU 3: COM ARQUIVO DE TEXTO / EBOOK
        # ---------------------------------------------------------------------
        else:
            ebook_path = Path(ebook_path)
            if not ebook_path.exists():
                raise FileNotFoundError(f"Arquivo não encontrado: {ebook_path}")

            ext = ebook_path.suffix.lower()
            if ext not in self.SUPPORTED_EXTENSIONS:
                raise ValueError(
                    f"Formato '{ext}' não suportado. Formatos aceitos: {sorted(self.SUPPORTED_EXTENSIONS)}"
                )

            # Caso 2.A: Áudio + Texto fornecidos -> Sincronização Dupla Real
            if real_audio_file and ext in (".epub", ".mobi", ".azw", ".prc", ".pdf", ".txt"):
                if log_callback:
                    log_callback(
                        f"[Modo Ideal]: Sincronização Dupla Real ('{ebook_path.name}' + '{real_audio_file.name}'). "
                        f"Garante fidelidade 100% da grafia do livro com tempos milimétricos da voz falada."
                    )
                audio_meta = inspect_audiobook_metadata(real_audio_file)
                if audio_meta.duration and (audio_duration is None or audio_duration <= 0):
                    audio_duration = audio_meta.duration

                if audio_meta.has_synchronized_text and audio_meta.segments:
                    if log_callback:
                        log_callback("Texto sincronizado encontrado nos metadados ID3 SYLT do áudio!")
                    base_segments = audio_meta.segments
                    sync_source = f"audio_{audio_meta.sync_source}"
                    has_native_timestamps = True
                else:
                    try:
                        import sync_audiobook

                        if log_callback:
                            log_callback(
                                f"Iniciando alinhamento acústico por blocos com a voz do áudio: '{real_audio_file.name}'..."
                            )
                        sync_data = sync_audiobook.synchronize_audiobook(
                            audio_path=real_audio_file,
                            ebook_path=ebook_path,
                            output_path=None,
                            model_name="tiny",
                            log_func=log_callback or print,
                        )
                        raw_segs = sync_data.get("segments") or sync_data.get("sentences") or []
                        raw_words = sync_data.get("words") or []
                        if raw_segs and raw_words:
                            base_segments = [
                                TimedItem(start=s["start"], end=s["end"], text=s["text"])
                                for s in raw_segs
                            ]
                            word_items = [
                                TimedItem(start=w["start"], end=w["end"], text=w["text"])
                                for w in raw_words
                            ]
                            has_native_timestamps = True
                            sync_source = "faster_whisper_alignment"
                    except Exception as ex_sync:
                        if log_callback:
                            log_callback(
                                f"Aviso no alinhamento por áudio ({ex_sync}). Recorrendo à cadência acústica de IA com pausas."
                            )

            # Caso 2.B: Apenas Texto ou Fallback de Texto -> Cadência Ideal de Leitura de IA
            if not base_segments:
                if log_callback:
                    log_callback(
                        "[Aviso]: Usando só o texto. A cadência e pausas foram estimadas para leitura de IA, "
                        "mas um áudio gravado pode conter entonações, pausas e velocidades que o app não tem como prever."
                    )
                    log_callback(
                        "[Recomendação]: O ideal para o melhor resultado é fornecer ambos (Texto + Áudio)."
                    )
                chapter_sections: List[ChapterSection] = []
                if custom_chapters:
                    chapter_sections = []
                    for idx_c, c in enumerate(custom_chapters, start=1):
                        tit = c.get("titulo") or c.get("title") or f"Capítulo {idx_c}"
                        conteudo = c.get("conteudo") or c.get("content") or ""
                        sec = ChapterSection(index=idx_c, title=tit)
                        heading_spoken = tit if tit.endswith(".") else f"{tit}."
                        sec.items.append(TimedItem(start=0.0, end=0.0, text=heading_spoken, chapter=idx_c, is_chapter_title=True))
                        for s in split_into_sentences(conteudo, ai_mode=ai_filter):
                            sec.items.append(TimedItem(start=0.0, end=0.0, text=s, chapter=idx_c, is_chapter_title=False))
                        chapter_sections.append(sec)
                    all_items = []
                    for sec in chapter_sections:
                        all_items.extend(sec.items)
                    base_segments = estimate_sentence_timings(
                        all_items, total_duration=audio_duration, words_per_minute=words_per_minute
                    )
                elif ext == ".epub":
                    items, has_smil, book_meta = extract_from_epub(ebook_path)
                    if has_smil:
                        has_native_timestamps = True
                        sync_source = "epub3_media_overlays_smil"
                        base_segments = items
                    else:
                        if require_native_timestamps:
                            raise ValueError(
                                "Este EPUB contém apenas texto e capítulos, mas não possui dados "
                                "temporais Media Overlays (SMIL) para sincronização automática."
                            )
                        all_text = "\n\n".join(it.text for it in items if it.text.strip())
                        chapter_sections = structure_text_into_chapters(all_text, ai_mode=ai_filter)
                        all_items = []
                        for sec in chapter_sections:
                            all_items.extend(sec.items)
                        base_segments = estimate_sentence_timings(
                            all_items, total_duration=audio_duration, words_per_minute=words_per_minute
                        )

                elif ext in (".mobi", ".azw", ".prc"):
                    if require_native_timestamps:
                        raise ValueError(
                            "Arquivos MOBI comuns não contêm dados temporais de narração embutidos."
                        )
                    text = extract_text_from_mobi(ebook_path)
                    chapter_sections = structure_text_into_chapters(text, ai_mode=ai_filter)
                    all_items = []
                    for sec in chapter_sections:
                        all_items.extend(sec.items)
                    base_segments = estimate_sentence_timings(
                        all_items, total_duration=audio_duration, words_per_minute=words_per_minute
                    )

                elif ext == ".pdf":
                    if require_native_timestamps:
                        raise ValueError("Arquivos PDF não contêm dados temporais de narração embutidos.")
                    text = extract_text_from_pdf(ebook_path)
                    chapter_sections = structure_text_into_chapters(text, ai_mode=ai_filter)
                    all_items = []
                    for sec in chapter_sections:
                        all_items.extend(sec.items)
                    base_segments = estimate_sentence_timings(
                        all_items, total_duration=audio_duration, words_per_minute=words_per_minute
                    )

                elif ext == ".txt":
                    if require_native_timestamps:
                        raise ValueError("Arquivos TXT não contêm dados temporais de narração embutidos.")
                    text = extract_text_from_txt(ebook_path)
                    chapter_sections = structure_text_into_chapters(text, ai_mode=ai_filter)
                    all_items = []
                    for sec in chapter_sections:
                        all_items.extend(sec.items)
                    base_segments = estimate_sentence_timings(
                        all_items, total_duration=audio_duration, words_per_minute=words_per_minute
                    )

                elif ext in (".lrc", ".vtt", ".srt"):
                    has_native_timestamps = True
                    sync_source = f"subtitle_{ext.lstrip('.')}"
                    base_segments = extract_from_lrc_or_vtt(ebook_path)

                elif ext == ".json":
                    loaded = json.loads(ebook_path.read_text(encoding="utf-8"))
                    has_native_timestamps = True
                    sync_source = "external_json"
                    if "segments" in loaded:
                        base_segments = [
                            TimedItem(
                                start=s["start"],
                                end=s["end"],
                                text=s["text"],
                                chapter=s.get("chapter"),
                                is_chapter_title=s.get("is_chapter_title", False),
                            )
                            for s in loaded["segments"]
                        ]
                    elif "words" in loaded:
                        word_objs = [
                            TimedItem(
                                start=w["start"],
                                end=w["end"],
                                text=w["text"],
                                chapter=w.get("chapter"),
                                is_chapter_title=w.get("is_chapter_title", False),
                            )
                            for w in loaded["words"]
                        ]
                        base_segments = group_words_into_sentences(word_objs)

        # ---------------------------------------------------------------------
        # MONTAGEM DA ESTRUTURA SINCRONIZADA
        # ---------------------------------------------------------------------
        audio_name = real_audio_file.name if real_audio_file else Path(audio_filename).name
        result_json: Dict[str, Any] = {"audio": audio_name}

        # Constrói metadados de capítulos agregados
        chapters_meta: List[Dict[str, Any]] = []
        if "chapter_sections" in locals() and chapter_sections:
            for sec in chapter_sections:
                sec_items = [it for it in base_segments if it.chapter == sec.index]
                if sec_items:
                    chapters_meta.append({
                        "index": sec.index,
                        "title": sec.title,
                        "start": round(sec_items[0].start, 2),
                        "end": round(sec_items[-1].end, 2),
                    })
        elif any(it.chapter is not None for it in base_segments):
            seen_c: Dict[int, Dict[str, Any]] = {}
            for it in base_segments:
                if it.chapter is not None:
                    if it.chapter not in seen_c:
                        c_title = (
                            it.text
                            if it.is_chapter_title
                            else ("0 - Introdução" if it.chapter == 0 else f"Capítulo {it.chapter}")
                        )
                        item_entry = {
                            "index": it.chapter,
                            "title": c_title.rstrip("."),
                            "start": round(it.start, 2),
                            "end": round(it.end, 2),
                        }
                        seen_c[it.chapter] = item_entry
                        chapters_meta.append(item_entry)
                    else:
                        seen_c[it.chapter]["end"] = max(seen_c[it.chapter]["end"], round(it.end, 2))

        if chapters_meta:
            result_json["chapters"] = chapters_meta

        if granularity == "sentence":
            result_json["segments"] = [seg.to_dict(decimals=2) for seg in base_segments]
        elif granularity == "word":
            if not word_items:
                word_items = subdivide_segments_into_words(base_segments)
            result_json["words"] = [w.to_dict(decimals=2) for w in word_items]
        elif granularity == "both":
            if not word_items:
                word_items = subdivide_segments_into_words(base_segments)
            result_json["segments"] = [seg.to_dict(decimals=2) for seg in base_segments]
            result_json["words"] = [w.to_dict(decimals=2) for w in word_items]
        else:
            raise ValueError(
                f"Granularidade inválida: '{granularity}'. Escolha 'sentence', 'word' ou 'both'."
            )

        # Validação temporal
        validation = validate_sync_data(result_json, audio_duration=audio_duration)

        if include_metadata:
            source_label = (
                ebook_path.name
                if ebook_path
                else (real_audio_file.name if real_audio_file else "audiobook")
            )
            result_json["metadata"] = {
                "source_file": source_label,
                "has_native_timestamps": has_native_timestamps,
                "sync_source": sync_source,
                "output_format": resolved_format,
                "ai_filter_applied": ai_filter,
                "words_per_minute": words_per_minute,
                "book_metadata": book_meta,
                "validation": {
                    "is_valid": validation.is_valid,
                    "total_items": validation.total_items,
                    "max_end_time": validation.max_end_time,
                    "errors": validation.errors,
                    "warnings": validation.warnings,
                },
            }

        # Exportação no formato escolhido (JSON, LRC, SRT, VTT, TXT)
        if destination_file:
            export_sync_data_to_format(
                result_json,
                output_format=resolved_format,
                output_path=destination_file,
                granularity=granularity,
            )

        return result_json


# =============================================================================
# 8. BUSCA BINÁRIA PARA O PLAYER (SINCRONIZAÇÃO EM TEMPO DE EXECUÇÃO)
# =============================================================================

def find_active_item_at_time(
    items: List[Dict[str, Any]], current_time: float
) -> Optional[Dict[str, Any]]:
    """
    Utilitário O(log N) para o player localizar rapidamente a frase ou palavra
    correspondente a `audio.currentTime` durante a reprodução.
    """
    low = 0
    high = len(items) - 1
    while low <= high:
        mid = (low + high) // 2
        item = items[mid]
        start = float(item["start"])
        end = float(item["end"])
        if start <= current_time <= end:
            return {"index": mid, **item}
        if current_time < start:
            high = mid - 1
        else:
            low = mid + 1
    return None


# =============================================================================
# 9. INTERFACE DE LINHA DE COMANDO (CLI)
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Converte ebooks (EPUB, MOBI, PDF, TXT) para formatos sincronizados de Audiobook."
    )
    parser.add_argument("input_file", nargs="?", default=None, help="Caminho do ebook (.epub, .mobi, .pdf, .txt) ou áudio")
    parser.add_argument(
        "--ebook",
        default=None,
        help="Caminho explícito do arquivo de ebook (.epub, .pdf, .txt, .mobi)",
    )
    parser.add_argument(
        "--audio",
        default=None,
        help="Nome ou caminho do arquivo de áudio correspondente (.mp3, .m4a, .wav, .ogg)",
    )
    parser.add_argument(
        "--format",
        default="json",
        choices=["json", "lrc", "srt", "vtt", "txt"],
        help="Formato do arquivo de saída gerado (padrão: json)",
    )
    parser.add_argument(
        "--mode",
        choices=["sentence", "word", "both"],
        default="both",
        help="Modo de destaque: 'sentence' (frases), 'word' (palavras) ou 'both' (ambos)",
    )
    parser.add_argument(
        "--wpm",
        type=float,
        default=135.0,
        help="Velocidade média em palavras por minuto (padrão: 135)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duração total do áudio em segundos (opcional)",
    )
    parser.add_argument(
        "--require-native-sync",
        action="store_true",
        help="Falha caso o ebook não possua sincronização nativa (ex.: EPUB sem SMIL)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Arquivo de saída (se omitido, imprime no terminal)",
    )
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="Inclui metadados e relatório de validação no arquivo gerado",
    )

    args = parser.parse_args()

    ebook_path = args.ebook
    audio_path = args.audio

    if args.input_file:
        p = Path(args.input_file)
        ext = p.suffix.lower()
        AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".wav", ".ogg", ".aac", ".flac"}
        if ext in AUDIO_EXTS and not audio_path:
            audio_path = args.input_file
        elif not ebook_path:
            ebook_path = args.input_file

    converter = EbookAudiobookConverter()
    data = converter.convert(
        ebook_path=ebook_path,
        audio_filename=Path(audio_path).name if audio_path else "audiobook.mp3",
        audio_path=audio_path,
        granularity=args.mode,
        words_per_minute=args.wpm,
        audio_duration=args.duration,
        require_native_timestamps=args.require_native_sync,
        output_path=args.output,
        output_format=args.format,
        include_metadata=args.include_metadata,
    )

    if args.output:
        print(f"Arquivo sincronizado salvo com sucesso em: {args.output}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
