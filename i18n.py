"""
Idiomas da interface: os textos ficam em dados/i18n/<idioma>.json (o inglês é a base e a reserva).

O idioma vem, nesta ordem, de --lang, da variável AUDIOBOOK_LANG ou do idioma do sistema
(Windows: idioma da interface; macOS: idiomas preferidos; Linux: LANGUAGE, LC_ALL, LC_MESSAGES, LANG).
Se o idioma do sistema não está na lista, a interface abre em inglês.
"""
import json
import locale
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PASTA = os.path.join(BASE, "dados", "i18n")
IDIOMAS = ["en", "es", "pt", "ru", "ar", "fr", "it", "tr", "hi", "zh", "ur"]
PADRAO = "en"

atual = PADRAO
_textos = {}
_reserva = {}


def _carregar(codigo):
    try:
        with open(os.path.join(PASTA, f"{codigo}.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _candidatos_do_sistema():
    """Idiomas do sistema, do mais preferido para o menos ('pt_BR.UTF-8', 'pt-BR', 'en'...)."""
    candidatos = []
    if os.name == "nt":
        try:
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            candidatos.append(locale.windows_locale.get(lcid, ""))
        except Exception:
            pass
    elif sys.platform == "darwin":
        # O Terminal do macOS costuma ter LANG=en_US mesmo com o sistema em outro idioma: vale a preferência do sistema
        try:
            r = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True, timeout=3)
            candidatos += re.findall(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*", r.stdout)
        except Exception:
            pass
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        candidatos += (os.environ.get(var) or "").split(":")
    try:
        candidatos.append(locale.getlocale()[0] or "")
    except Exception:
        pass
    return candidatos


def _codigo(valor):
    """'pt_BR.UTF-8', 'zh-Hans-CN', 'Portuguese_Brazil' -> 'pt', 'zh', ... ('' se não der para saber)."""
    v = str(valor or "").split(".")[0].replace("-", "_").split("_")[0].strip().lower()
    if v in ("", "c", "posix"):
        return ""
    nomes = {"english": "en", "spanish": "es", "portuguese": "pt", "russian": "ru", "arabic": "ar", "french": "fr",
             "italian": "it", "turkish": "tr", "hindi": "hi", "chinese": "zh", "urdu": "ur"}
    return nomes.get(v, v)


def idioma_do_sistema():
    for c in _candidatos_do_sistema():
        codigo = _codigo(c)
        if codigo:  # o primeiro idioma conhecido do sistema decide; fora da lista, inglês
            return codigo if codigo in IDIOMAS else PADRAO
    return PADRAO


def configurar(escolhido=None):
    """Define o idioma da interface. Devolve o código usado."""
    global atual, _textos, _reserva
    codigo = _codigo(escolhido or os.environ.get("AUDIOBOOK_LANG"))
    if codigo not in IDIOMAS:
        codigo = idioma_do_sistema()
    atual = codigo
    _reserva = _carregar(PADRAO)
    _textos = _carregar(codigo) if codigo != PADRAO else _reserva
    return codigo


def t(chave, *args, **kwargs):
    texto = _textos.get(chave) or _reserva.get(chave) or chave
    try:
        return texto.format(*args, **kwargs) if (args or kwargs) else texto
    except (IndexError, KeyError, ValueError):
        return texto


def palavras(chave):
    """Lista de respostas aceitas ('s,sim' -> ['s', 'sim'])."""
    return [p.strip().lower() for p in t(chave).split(",") if p.strip()]


def direita_para_esquerda():
    return atual in ("ar", "ur")


# ---------------------------------------------------------------------------------------------
# Mensagens do motor (ele fala português: aqui viram o idioma da interface)
# ---------------------------------------------------------------------------------------------
_MOTOR = {
    "Lendo o livro": "engine_reading", "Traduzindo o texto": "engine_translating",
    "Preparando o texto": "engine_preparing", "Ajustando o volume dos capítulos": "engine_volume",
    "Narrando": "engine_narrating", "Gerando o texto sincronizado": "engine_sync",
    "Texto sincronizado pronto": "engine_sync_done", "Gerando os arquivos dos capítulos": "engine_chapter_files",
    "trechos traduzidos": "unit_translated", "capítulos com volume ajustado": "unit_volume",
    "partes narradas": "unit_narrated", "arquivos de capítulo gerados": "unit_chapter_files",
    "Não foi possível ler o texto deste arquivo (ele pode estar protegido ou ser só imagens).": "err_unreadable",
}
_ETAPAS = {"tradução": "stage_translation", "preparação do texto": "stage_preparation", "narração": "stage_narration",
           "montagem do áudio e texto sincronizado": "stage_sync", "início": "stage_start"}
_MOTOR_RE = [
    (re.compile(r"Montando o arquivo (\S+)$"), "engine_building"),
    (re.compile(r"capítulos no (\S+)$"), "unit_chapters_in"),
    (re.compile(r"Tradução: nova tentativa em (\d+)s \((\d+)/5\)"), "engine_retry_translation"),
    (re.compile(r"Voz: nova tentativa (\d+)/8 em (\d+)s"), "engine_retry_voice"),
    (re.compile(r"Não foi possível gravar na pasta de destino \((.*)\)\.", re.S), "err_write"),
    (re.compile(r"Espaço insuficiente na pasta de destino: são necessários cerca de ([\d.]+) GB e há ([\d.]+) GB livres"), "err_space"),
    (re.compile(r"Este livro já está sendo convertido"), "err_busy"),
]
_FALHA_RE = re.compile(r"A conversão falhou na etapa de (.+?) \((\w+)\): (.*)", re.S)


def do_motor(msg):
    """Traduz uma mensagem do motor; o que não é conhecido passa como veio."""
    if not msg:
        return msg
    msg = str(msg)
    limpa = msg.rstrip(".…") if msg.rstrip(".…") in _MOTOR else msg
    if limpa in _MOTOR:
        return t(_MOTOR[limpa])
    m = _FALHA_RE.match(msg)
    if m:
        etapa = t(_ETAPAS[m.group(1)]) if m.group(1) in _ETAPAS else m.group(1)
        return t("err_stage", etapa, m.group(2), m.group(3))
    for padrao, chave in _MOTOR_RE:
        m = padrao.search(msg)
        if m:
            return t(chave, *m.groups())
    return msg
