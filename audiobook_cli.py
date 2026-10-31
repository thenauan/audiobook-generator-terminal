"""
Audiobook Generator para o terminal: converte e-books (EPUB, PDF, MOBI, AZW3, TXT) em audiobooks (M4B, MP3...)
com o mesmo motor de conversão do app, sem interface gráfica. A interface abre no idioma do sistema
(inglês, se ele não estiver entre os idiomas de dados/i18n).

Uso:
    python audiobook_cli.py                       abre o seletor de arquivos
    python audiobook_cli.py livro.epub outro.pdf  já começa com esses livros
    python audiobook_cli.py livro.epub -o "C:\\Audiobooks"
    python audiobook_cli.py --lang pt             interface em português
    python audiobook_cli.py *.epub --voice pt-BR-AntonioNeural --format mp3 --yes   sem perguntas
"""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)  # instalado com pip/pipx, os módulos ao lado (ui, i18n) também são achados
MOTOR = os.path.join(BASE, "motor")
sys.path.insert(1, MOTOR)

import atualizacao  # noqa: E402
import i18n  # noqa: E402
import ui  # noqa: E402
from i18n import t  # noqa: E402

VERSAO = "1.5.0"

EXTENSOES = (".epub", ".pdf", ".mobi", ".azw", ".azw3", ".prc", ".txt")
TOTAL_ETAPAS = 6

# Idiomas para os quais dá para traduzir (os da interface, todos com vozes; o motor traduz para o idioma da voz)
DESTINOS_TRADUCAO = i18n.IDIOMAS
# Voz sugerida por idioma (a primeira da lista quando o idioma não está aqui)
VOZ_PREFERIDA = {"pt": "pt-BR-AntonioNeural", "en": "en-US-AndrewNeural", "es": "es-ES-AlvaroNeural",
                 "ar": "ar-SA-HamedNeural", "ru": "ru-RU-SvetlanaNeural", "fr": "fr-FR-HenriNeural",
                 "de": "de-DE-ConradNeural", "it": "it-IT-DiegoNeural", "tr": "tr-TR-AhmetNeural",
                 "hi": "hi-IN-MadhurNeural", "zh": "zh-CN-YunxiNeural", "ur": "ur-PK-AsadNeural"}
LOCALE_PREFERIDO = {"pt": "pt-BR", "en": "en-US", "es": "es-ES", "ar": "ar-SA", "zh": "zh-CN", "fr": "fr-FR",
                    "it": "it-IT", "ru": "ru-RU", "tr": "tr-TR", "hi": "hi-IN", "ur": "ur-PK", "de": "de-DE"}
FRASE_AMOSTRA = {
    "pt": "Olá! Esta é a minha voz. Que tal ouvirmos juntos uma boa história?",
    "en": "Hello! This is my voice. Shall we listen to a good story together?",
    "es": "¡Hola! Esta es mi voz. ¿Escuchamos juntos una buena historia?",
    "ar": "مرحبًا! هذا هو صوتي. هل نستمع معًا إلى قصة جميلة؟",
    "ru": "Привет! Это мой голос. Давайте послушаем хорошую историю вместе.",
    "fr": "Bonjour ! Voici ma voix. Et si nous écoutions ensemble une belle histoire ?",
    "it": "Ciao! Questa è la mia voce. Ascoltiamo insieme una bella storia?",
    "tr": "Merhaba! Bu benim sesim. Birlikte güzel bir hikâye dinleyelim mi?",
    "hi": "नमस्ते! यह मेरी आवाज़ है। क्या हम साथ में एक अच्छी कहानी सुनें?",
    "zh": "你好！这是我的声音。我们一起听一个好故事吧？",
    "ur": "السلام علیکم! یہ میری آواز ہے۔ کیا ہم مل کر ایک اچھی کہانی سنیں؟",
    "de": "Hallo! Das ist meine Stimme. Wollen wir zusammen eine gute Geschichte hören?",
}
# (formato do motor, chave do nome, chave da descrição); o nome sem chave de tradução é o próprio formato
FORMATOS = [("m4b", "fmt_m4b_name", "fmt_m4b_desc"), ("single", "fmt_mp3_name", "fmt_mp3_desc"),
            ("multiple", "fmt_multi_name", "fmt_multi_desc"), ("opus", "Opus", "fmt_opus_desc"),
            ("flac", "FLAC", "fmt_flac_desc"), ("wav", "WAV", "fmt_wav_desc")]
FORMATOS_SEM_FFMPEG = ("single", "multiple")  # sem FFmpeg, o MP3 sai juntando a voz como veio
# --format na linha de comando -> formato do motor
FORMATOS_CLI = {"m4b": "m4b", "mp3": "single", "mp3-chapters": "multiple", "opus": "opus", "flac": "flac", "wav": "wav"}
# "voice": do tamanho da voz de origem (metade de "high", mesmo som); as outras sobem a taxa
QUALIDADES = ("voice", "standard", "high", "max")

with open(os.path.join(BASE, "dados", "vozes.json"), encoding="utf-8") as f:
    VOZES = json.load(f)  # [{c: ShortName, l: Locale, g: F|M}]
with open(os.path.join(BASE, "dados", "idiomas.json"), encoding="utf-8") as f:
    _DADOS_IDIOMAS = json.load(f)
IDIOMAS = _DADOS_IDIOMAS["idiomas"]   # código -> {nativo, en, pt, es...}
REGIOES = _DADOS_IDIOMAS["regioes"]   # BR -> {en: Brazil, pt: Brasil...}


# ---------------------------------------------------------------------------------------------
# Últimas escolhas (voz por idioma, formato, qualidade, pasta, texto sincronizado), sugeridas na próxima vez
# ---------------------------------------------------------------------------------------------
def caminho_config():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Audiobook Generator Terminal", "config.json")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Audiobook Generator Terminal/config.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "audiobook-generator-terminal", "config.json")


def ler_config():
    try:
        with open(caminho_config(), encoding="utf-8") as f:
            dados = json.load(f)
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def salvar_config(config):
    try:
        os.makedirs(os.path.dirname(caminho_config()), exist_ok=True)
        with open(caminho_config(), "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # sem onde guardar: só não sugere as escolhas na próxima vez


# ---------------------------------------------------------------------------------------------
# Idiomas e vozes
# ---------------------------------------------------------------------------------------------
def _sem_acento(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c)).lower().strip()


_POR_NOME = {}
for _cod, _n in IDIOMAS.items():
    for _nome in _n.values():
        _POR_NOME.setdefault(_sem_acento(_nome), _cod)


def codigo_idioma(nome):
    """'Português (Brasil)', 'English', 'Grego', 'el', 'sv-SE' -> código de duas letras ('' se não reconhecer)."""
    n = _sem_acento(re.sub(r"\s*\(.*\)$", "", str(nome or "")))
    if not n:
        return ""
    m = re.fullmatch(r"([a-z]{2,3})(?:[-_][a-z0-9]+)*", n)
    if m and m.group(1) in IDIOMAS:
        return m.group(1)
    return _POR_NOME.get(n, "")


def nome_idioma(codigo):
    """Nome do idioma na língua da interface ('Portuguese', 'Português', 'Португальский'...)."""
    n = IDIOMAS.get(codigo, {})
    return n.get(i18n.atual) or n.get("nativo") or codigo or "?"


def nome_idioma_frase(codigo):
    """Nome do idioma no meio de uma frase: minúsculo nas línguas em que é assim ('narrar em português')."""
    nome = nome_idioma(codigo)
    return nome.lower() if i18n.atual in ("pt", "es", "fr", "it", "ru") else nome


def nome_regiao(regiao):
    n = REGIOES.get(regiao)
    return (n.get(i18n.atual) or n.get("en") or regiao) if isinstance(n, dict) else (n or regiao)


def nome_voz(v):
    nome = v["c"][len(v["l"]) + 1:].replace("Neural", "").replace("Multilingual", f" ({t('multilingual')})").strip()
    regiao = next((p for p in v["l"].split("-") if re.fullmatch(r"[A-Z]{2}", p)), "")
    genero = t("female") if v["g"] == "F" else t("male")
    onde = nome_regiao(regiao) if regiao else ""
    return nome, f"{genero} · {nome_idioma(v['l'].split('-')[0])}" + (f" ({onde})" if onde else "")


def vozes_do_idioma(codigo):
    lista = [v for v in VOZES if v["l"].split("-")[0] == codigo]
    pref = LOCALE_PREFERIDO.get(codigo)
    return sorted(lista, key=lambda v: (v["l"] != pref, v["l"]))


# ---------------------------------------------------------------------------------------------
# Seletores do sistema (arquivos e pasta)
# ---------------------------------------------------------------------------------------------
def _tk():
    try:
        import tkinter
        from tkinter import filedialog
        raiz = tkinter.Tk()
        raiz.withdraw()
        raiz.attributes("-topmost", True)
        return raiz, filedialog
    except Exception:
        return None, None


def escolher_arquivos():
    raiz, dialogo = _tk()
    if raiz:
        ui.dica(t("opening_file_picker"))
        caminhos = dialogo.askopenfilenames(
            parent=raiz, title=t("file_picker_title"),
            filetypes=[(t("filetype_ebooks"), " ".join(f"*{e}" for e in EXTENSOES)), (t("filetype_all"), "*.*")])
        raiz.destroy()
        if caminhos:
            return list(caminhos)
        ui.alerta(t("no_file_chosen"))
    ui.dica(t("type_or_drag"))
    caminhos = []
    while True:
        linha = ui.perguntar(t("prompt_file")).strip()
        if not linha:
            return caminhos
        caminhos.extend(caminhos_digitados(linha))


def caminhos_digitados(linha):
    """Caminhos como o terminal os cola ao arrastar arquivos: entre aspas ("C:\\Meus livros\\a.epub"
    no Windows, '/Users/eu/a b.epub' no macOS) ou com espaços escapados (/home/eu/a\\ b.epub no Linux)."""
    if os.path.exists(os.path.expanduser(linha.strip("\"'"))):
        return [os.path.expanduser(linha.strip("\"'"))]
    try:
        partes = shlex.split(linha, posix=os.name != "nt")
    except ValueError:
        partes = [linha]
    return [os.path.expanduser(p.strip("\"'")) for p in partes]


def _pasta_documentos():
    casa = os.path.expanduser("~")
    if sys.platform.startswith("linux"):
        try:  # pasta Documentos do usuário, no idioma do sistema (XDG)
            r = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True, timeout=3)
            pasta = r.stdout.strip()
            if pasta and pasta != casa and os.path.isdir(pasta):
                return pasta
        except Exception:
            pass
    for nome in ("Documents", "Documentos"):
        if os.path.isdir(os.path.join(casa, nome)):
            return os.path.join(casa, nome)
    return casa


def pasta_padrao():
    return os.path.join(_pasta_documentos(), "Audiobook Generator")


def escolher_pasta(sugerida=None):
    padrao = sugerida or pasta_padrao()
    raiz, dialogo = _tk()
    if raiz:
        ui.dica(t("opening_folder_picker"))
        os.makedirs(padrao, exist_ok=True)
        pasta = dialogo.askdirectory(parent=raiz, title=t("folder_picker_title"), initialdir=padrao, mustexist=False)
        raiz.destroy()
        if pasta:
            return os.path.normpath(pasta)
        ui.dica(t("no_folder_chosen"))
        return padrao
    pasta = ui.perguntar(t("prompt_folder"), padrao)
    return os.path.normpath((caminhos_digitados(pasta) or [padrao])[0])


def abrir(caminho):
    try:
        if os.name == "nt":
            os.startfile(caminho)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", caminho])
        else:
            subprocess.Popen(["xdg-open", caminho], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        ui.alerta(t("cannot_open", error=e))


# ---------------------------------------------------------------------------------------------
# Verificações
# ---------------------------------------------------------------------------------------------
def verificar_dependencias():
    """Confere as bibliotecas sem importá-las (abrir pdfplumber e lxml só para conferir atrasava a abertura).
    Devolve se o FFmpeg está instalado: sem ele, só os formatos MP3."""
    faltando = []
    for modulo, pacote in [("edge_tts", "edge-tts"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"), ("mutagen", "mutagen"),
                           ("pypdf", "pypdf"), ("pdfplumber", "pdfplumber"), ("PIL", "pillow"), ("mobi", "mobi"), ("langdetect", "langdetect")]:
        try:
            if importlib.util.find_spec(modulo) is None:
                faltando.append(pacote)
        except (ImportError, ValueError):
            faltando.append(pacote)
    if faltando:
        ui.falha(t("missing_libs", libs=", ".join(faltando)))
        python = "python" if os.name == "nt" else "python3"
        ui.dica(t("install_with", command=f"{python} -m pip install -r requirements.txt"))
        sys.exit(1)
    if shutil.which("ffmpeg"):
        return True
    ui.alerta(t("ffmpeg_missing_mp3"))
    if os.name == "nt":
        comando = "winget install Gyan.FFmpeg"
    elif sys.platform == "darwin":
        comando = "brew install ffmpeg"
    else:
        comando = "sudo apt install ffmpeg  " + t("ffmpeg_linux_hint")
    ui.dica(t("ffmpeg_install_for_more", command=comando))
    return False


# ---------------------------------------------------------------------------------------------
# Leitura dos livros
# ---------------------------------------------------------------------------------------------
def id_do_arquivo(caminho):
    """Nome da pasta de trabalho do livro pelo arquivo de origem (caminho e tamanho), e não pelo título:
    editar o título não perde o que já foi narrado."""
    try:
        tamanho = os.path.getsize(caminho)
    except OSError:
        tamanho = 0
    chave = f"{os.path.normcase(os.path.abspath(caminho))}|{tamanho}"
    return "livro_" + hashlib.sha1(chave.encode("utf-8")).hexdigest()[:16]


def ler_livros(caminhos):
    from extract_metadata import extract_metadata
    livros, prontos = [], [0]
    trava = threading.Lock()

    def ler(caminho):
        try:
            dados = extract_metadata(caminho, os.path.basename(caminho))
        except Exception as e:
            dados = {"unreadable": t("cannot_read_file", error=e)}
        with trava:
            prontos[0] += 1
            sys.stdout.write(f"\r\033[2K  {ui.destaque(ui.ICONE['info'])} " + t("reading_books", done=prontos[0], total=len(caminhos)))
            sys.stdout.flush()
        return caminho, dados

    with ThreadPoolExecutor(max_workers=min(4, len(caminhos))) as ex:
        resultados = list(ex.map(ler, caminhos))
    sys.stdout.write("\r\033[2K")
    for caminho, d in resultados:
        nome = os.path.basename(caminho)
        if d.get("unreadable"):
            ui.falha(f"{nome}: {i18n.do_motor(d['unreadable'])}")
            continue
        texto_inicio = ""
        for cap in d.get("chapters") or []:
            conteudo = (cap.get("content") or "").strip()
            if len(conteudo) > 200:
                texto_inicio = conteudo
                break
        autor = d.get("author") or ""
        livros.append({
            "id": uuid.uuid4().hex[:10], "caminho": caminho, "arquivo": nome, "cache_id": id_do_arquivo(caminho),
            "titulo": d.get("title") or os.path.splitext(nome)[0],
            "autor": t("unknown_author") if autor in ("", "Desconhecido") else autor,
            "genero": d.get("genre") or "", "subgenero": d.get("subgenre") or "", "ano": d.get("year") or "",
            "idioma": codigo_idioma(d.get("language")), "capitulos": len(d.get("chapters") or []),
            "duracao": d.get("durationEst") or "", "capa": d.get("cover"), "trecho": texto_inicio,
        })
        ui.ok(f"{nome} {ui.suave('· ' + t('n_chapters', n=len(d.get('chapters') or [])))}")
    return livros


# ---------------------------------------------------------------------------------------------
# Amostra de voz (gerada na hora, com um trecho do próprio livro quando dá)
# ---------------------------------------------------------------------------------------------
def tocar_amostra(voz, idioma, trecho=""):
    import edge_tts
    if trecho:
        frase = re.split(r"(?<=[.!?…。！？])\s*", re.sub(r"\s+", " ", trecho))[:2]
        frase = " ".join(frase)[:260]
    else:
        frase = FRASE_AMOSTRA.get(idioma, "1, 2, 3.")
    destino = os.path.join(tempfile.gettempdir(), f"amostra_{voz}.mp3")
    ui.dica(f"{ui.ICONE['amostra']} " + t("generating_sample", voice=voz))
    try:
        asyncio.run(edge_tts.Communicate(frase, voz).save(destino))
        abrir(destino)
    except Exception as e:
        ui.alerta(t("sample_failed", error=type(e).__name__))


# ---------------------------------------------------------------------------------------------
# Escolhas de cada livro
# ---------------------------------------------------------------------------------------------
def revisar_informacoes(livro):
    ui.cartao(livro["titulo"], [
        (t("field_author"), livro["autor"]), (t("field_genre"), livro["genero"]), (t("field_subgenre"), livro["subgenero"]),
        (t("field_year"), livro["ano"]),
        (t("field_language"), nome_idioma(livro["idioma"]) if livro["idioma"] else t("not_recognized")),
        (t("field_chapters"), str(livro["capitulos"])), (t("field_duration"), livro["duracao"]), (t("field_file"), livro["arquivo"]),
    ])
    if not ui.sim_nao(t("edit_details"), padrao=False):
        return
    livro["titulo"] = ui.perguntar(t("field_title"), livro["titulo"])
    livro["autor"] = ui.perguntar(t("field_author"), livro["autor"])
    livro["genero"] = ", ".join(g.strip() for g in ui.perguntar(t("prompt_genres"), livro["genero"]).split(",") if g.strip())
    livro["subgenero"] = ", ".join(g.strip() for g in ui.perguntar(t("prompt_subgenres"), livro["subgenero"]).split(",") if g.strip())
    while True:
        ano = ui.perguntar(t("field_year"), livro["ano"])
        if not ano or re.fullmatch(r"\d{4}", ano):
            livro["ano"] = ano
            break
        ui.alerta(t("year_invalid"))
    while True:
        idioma = ui.perguntar(t("prompt_book_language"), nome_idioma(livro["idioma"]) if livro["idioma"] else "")
        codigo = codigo_idioma(idioma)
        if codigo or not idioma:
            livro["idioma"] = codigo
            break
        ui.alerta(t("language_invalid"))
    ui.ok(t("details_updated"))


def escolher_traducao(livro):
    if not livro["idioma"]:
        return ""
    destinos = [c for c in DESTINOS_TRADUCAO if c != livro["idioma"]]
    opcoes = [(t("no_translation"), t("narrate_in", language=nome_idioma_frase(livro["idioma"])))] + \
             [(t("translate_to", language=nome_idioma_frase(c)), IDIOMAS[c]["nativo"]) for c in destinos]
    print()
    ui.info(f"{ui.ICONE['traduzir']} " + t("translation"))
    i = ui.escolher(t("prompt_option"), opcoes, padrao=0, colunas=1 if len(opcoes) <= 6 else 2)
    return "" if i == 0 else destinos[i - 1]


def escolher_idioma_narracao():
    codigos = sorted(IDIOMAS, key=lambda c: _sem_acento(nome_idioma(c)))
    ui.alerta(t("language_unknown_choose"))
    padrao = codigos.index(i18n.atual) if i18n.atual in codigos else 0
    i = ui.escolher(t("prompt_language"), [(nome_idioma(c), "") for c in codigos], padrao=padrao, colunas=3)
    return codigos[i]


def voz_padrao(idioma, config):
    """A última voz usada neste idioma; senão a sugerida do idioma; senão a primeira."""
    codigos = [v["c"] for v in vozes_do_idioma(idioma)]
    for voz in ((config.get("vozes") or {}).get(idioma), VOZ_PREFERIDA.get(idioma)):
        if voz in codigos:
            return voz
    return codigos[0] if codigos else None


def voz_automatica(idioma, voz_pedida, config):
    """Voz sem perguntar (--voice / --yes): a pedida, se narra este idioma; senão a padrão do idioma."""
    if not vozes_do_idioma(idioma):
        idioma = i18n.atual if vozes_do_idioma(i18n.atual) else "en"
    padrao = voz_padrao(idioma, config)
    if voz_pedida and voz_pedida not in (v["c"] for v in vozes_do_idioma(idioma)):
        ui.alerta(t("voice_ignored", voice=voz_pedida, language=nome_idioma_frase(idioma), default=padrao))
        return padrao
    return voz_pedida or padrao


def escolher_voz(idioma, trecho, sugerida=None):
    todas = vozes_do_idioma(idioma)
    if not todas:
        ui.alerta(t("no_voices", language=nome_idioma_frase(idioma)))
        return escolher_voz(escolher_idioma_narracao(), "", None)
    if sugerida not in (v["c"] for v in todas):
        sugerida = VOZ_PREFERIDA.get(idioma) if VOZ_PREFERIDA.get(idioma) in (v["c"] for v in todas) else todas[0]["c"]
    genero, regiao = None, None  # filtros: f/m (gênero), r XX (região), t (todas)
    while True:
        vozes = [v for v in todas if (not genero or v["g"] == genero) and (not regiao or regiao in v["l"].split("-"))]
        if not vozes:
            ui.alerta(t("no_voices_filter"))
            genero, regiao = None, None
            continue
        codigos = [v["c"] for v in vozes]
        padrao = codigos.index(sugerida) if sugerida in codigos else 0
        print()
        ui.info(f"{ui.ICONE['voz']} " + t("voice_heading", language=nome_idioma_frase(idioma)) + " " + ui.suave(t("n_voices", n=len(vozes))))
        extras = {"o": t("listen_hint")}
        if len(todas) > 6:  # filtros só quando há vozes que valha filtrar
            extras.update({"f": t("filter_female"), "m": t("filter_male"), "r": t("filter_region"), "t": t("filter_all")})
        r = ui.escolher(t("prompt_voice"), [nome_voz(v) for v in vozes], padrao=padrao, extras=extras,
                        colunas=1 if len(vozes) <= 12 else 2)
        if not isinstance(r, tuple):
            return codigos[r]
        tecla, argumento = r[0], r[1].strip()
        if tecla == "o":
            n = argumento or str(padrao + 1)
            if n.isdigit() and 1 <= int(n) <= len(vozes):
                sugerida = codigos[int(n) - 1]
                tocar_amostra(sugerida, idioma, trecho)
            else:
                ui.alerta(t("listen_usage", n=padrao + 1))
        elif tecla in ("f", "m"):
            genero = tecla.upper()
        elif tecla == "r":
            regiao = argumento.upper() or None
        elif tecla == "t":
            genero, regiao = None, None


def configurar_livros(livros, opcoes):
    """Informações, tradução, texto sincronizado e voz de cada livro. `opcoes`: o que veio da linha de comando
    (sem_perguntas, voz, traduzir, texto) e as últimas escolhas salvas (config)."""
    config = opcoes["config"]
    voz_pedida = opcoes["voz"]
    idioma_voz_pedida = next((v["l"].split("-")[0] for v in VOZES if v["c"] == voz_pedida), None)
    texto_padrao = config.get("texto", True) if opcoes["texto"] is None else opcoes["texto"]
    escolhas_por_idioma = {}  # idioma -> (traduzir, texto, voz), quando o usuário quer repetir
    for n, livro in enumerate(livros, 1):
        if opcoes["sem_perguntas"]:
            destino = opcoes["traduzir"]
            livro["traduzir"] = destino if destino and destino != livro["idioma"] else ""
            livro["texto"] = texto_padrao
            idioma_narracao = livro["traduzir"] or livro["idioma"] or idioma_voz_pedida or i18n.atual
            livro["voz"] = voz_automatica(idioma_narracao, voz_pedida, config)
            continue
        print()
        ui.info(ui.negrito(t("book_n_of", n=n, total=len(livros))))
        revisar_informacoes(livro)
        repetir = escolhas_por_idioma.get(livro["idioma"]) if livro["idioma"] else None
        if repetir:
            livro["traduzir"], livro["texto"], livro["voz"] = repetir
            ui.ok(t("reusing_choices", language=nome_idioma_frase(livro["idioma"]),
                    translation=t("translate_to_lower", language=nome_idioma_frase(livro["traduzir"])) if livro["traduzir"] else t("without_translation"),
                    sync=t("with_sync") if livro["texto"] else t("without_sync"),
                    voice=nome_voz(next(v for v in VOZES if v["c"] == livro["voz"]))[0]))
            continue
        if opcoes["traduzir"]:
            livro["traduzir"] = opcoes["traduzir"] if opcoes["traduzir"] != livro["idioma"] else ""
        else:
            livro["traduzir"] = escolher_traducao(livro)
        if opcoes["texto"] is None:
            print()
            livro["texto"] = ui.sim_nao(f"{ui.ICONE['texto']} " + t("ask_sync"), padrao=texto_padrao)
        else:
            livro["texto"] = opcoes["texto"]
        idioma_narracao = livro["traduzir"] or livro["idioma"] or idioma_voz_pedida or escolher_idioma_narracao()
        if voz_pedida:
            livro["voz"] = voz_automatica(idioma_narracao, voz_pedida, config)
        else:
            livro["voz"] = escolher_voz(idioma_narracao, "" if livro["traduzir"] else livro["trecho"], voz_padrao(idioma_narracao, config))
        restantes = [l for l in livros[n:] if l["idioma"] == livro["idioma"]]
        if livro["idioma"] and restantes and ui.sim_nao(
                t("reuse_choices_q", n=len(restantes), language=nome_idioma_frase(livro["idioma"])), padrao=True):
            escolhas_por_idioma[livro["idioma"]] = (livro["traduzir"], livro["texto"], livro["voz"])


def nome_formato(formato):
    return t(next(nome for f, nome, _ in FORMATOS if f == formato))


def escolher_formato(tem_ffmpeg, sugerido):
    opcoes = [f for f in FORMATOS if tem_ffmpeg or f[0] in FORMATOS_SEM_FFMPEG]
    padrao = next((i for i, f in enumerate(opcoes) if f[0] == sugerido), 0)
    i = ui.escolher(t("prompt_format"), [(t(nome), t(desc)) for _, nome, desc in opcoes], padrao=padrao)
    return opcoes[i][0]


# ---------------------------------------------------------------------------------------------
# Conversão (o mesmo motor do app: converter.py, com os eventos de progresso em JSON)
# ---------------------------------------------------------------------------------------------
NOME_ETAPA = {"translating": "engine_translating", "editing": "engine_preparing", "syncing": "engine_sync"}


def converter(livros, formato, pasta, qualidade="voice", modo=None):
    """`modo`: None, "lite" (um livro por vez, leve para o computador) ou "turbo" (usa mais CPU e conexões)."""
    config = {
        "outputFolder": pasta, "file_format": formato, "fileType": formato, "audio_quality": qualidade,
        "sync_text_destination": "both", "sync_text_granularity": "sentences",
        "lite": modo == "lite", "turbo": modo == "turbo",
        "files": [{
            "id": l["id"], "path": os.path.abspath(l["caminho"]), "filename": l["arquivo"], "title": l["titulo"],
            "cache_id": l["cache_id"],
            "author": l["autor"], "genre": l["genero"], "subgenre": l["subgenero"], "year": l["ano"],
            # O motor recebe o nome do idioma em português, como no app
            "language": IDIOMAS.get(l["idioma"], {}).get("pt", "") if l["idioma"] else "", "voice": l["voz"],
            "translate": bool(l["traduzir"]), "fileType": formato, "file_format": formato, "cover": l["capa"],
            "generic_cover": True,  # e-book sem capa: o motor gera a capa genérica do app
            "chapters": [], "generate_sync_text": l["texto"], "sync_text_destination": "both",
        } for l in livros],
    }
    cfg = os.path.join(tempfile.gettempdir(), f"audiobook_cli_{uuid.uuid4().hex[:8]}.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)

    painel = ui.PainelProgresso([{"id": l["id"], "titulo": l["titulo"]} for l in livros])
    resultados = {l["id"]: {"ok": False, "erro": None, "detalhes": None, "arquivos": []} for l in livros}
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    # Grupo de processos próprio (o motor e o FFmpeg que ele abre): o Ctrl+C fica só com este app,
    # que pergunta antes de cancelar, e o cancelamento encerra o grupo inteiro
    if os.name == "nt":
        grupo = {"creationflags": 0x00000200}  # CREATE_NEW_PROCESS_GROUP
    else:
        grupo = {"start_new_session": True}
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(MOTOR, "converter.py"), cfg], cwd=MOTOR,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=env, **grupo)
    erros_motor = []
    threading.Thread(target=lambda: erros_motor.extend(proc.stderr.readlines()), daemon=True).start()

    def ler_eventos():
        ultimo_desenho = 0.0
        for linha in proc.stdout:
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            fid, tipo = ev.get("fileId"), ev.get("type")
            if tipo == "progress":
                # Uma porcentagem só (todas as etapas embutidas); ao lado, a contagem da etapa atual
                st = ev.get("status")
                unidade = i18n.do_motor(ev.get("partsUnit"), ev.get("unitCode"), ev.get("unitArgs")) or t("parts")
                partes = f"{ev['partsDone']}/{ev['partsTotal']} {unidade}" if ev.get("partsTotal") else ""
                etapa = i18n.do_motor((ev.get("message") or "").rstrip(".…"), ev.get("code"), ev.get("args")) \
                    or t(NOME_ETAPA.get(st, "engine_narrating"))
                painel.atualizar(fid, pct=float(ev.get("progress") or 0), etapa=etapa, partes=partes)
            elif tipo == "speed":
                bps = ev.get("bytesPerSec") or 0
                painel.atualizar(fid, vel=(f"{bps / 1048576:.1f} MB/s" if bps >= 1048576 else f"{bps // 1024} KB/s") if bps else "")
            elif tipo == "done":
                painel.atualizar(fid, pct=100.0, fim="ok")
                resultados[fid].update(ok=True, arquivos=ev.get("paths") or [])
            elif tipo == "error":
                erro = i18n.do_motor(ev.get("error"), ev.get("code"), ev.get("args")) or t("conversion_failed")
                painel.atualizar(fid, fim="erro", etapa=erro)
                resultados[fid].update(erro=erro, detalhes=ev.get("details"))
            if time.time() - ultimo_desenho > 0.2 or tipo in ("done", "error"):
                painel.desenhar()
                ultimo_desenho = time.time()

    leitor = threading.Thread(target=ler_eventos, daemon=True)
    leitor.start()
    painel.desenhar()
    cancelado = False
    # O Ctrl+C só anota o pedido (uma exceção poderia escapar entre duas linhas e largar o motor rodando);
    # o laço abaixo vê o pedido e pergunta antes de cancelar
    pedido = threading.Event()
    padrao_sigint = signal.getsignal(signal.SIGINT)

    def anotar_pedido(*_):
        pedido.set()

    signal.signal(signal.SIGINT, anotar_pedido)
    try:
        while leitor.is_alive():
            leitor.join(0.3)
            if pedido.is_set():
                if confirmar_cancelamento(painel, padrao_sigint):
                    cancelado = True
                    encerrar_motor(proc)
                    break
                signal.signal(signal.SIGINT, anotar_pedido)
                pedido.clear()
        proc.wait()
        leitor.join(5)
    finally:
        signal.signal(signal.SIGINT, padrao_sigint)
    if cancelado:
        for fid, r in resultados.items():
            if not r["ok"] and not r["erro"]:
                painel.atualizar(fid, fim="erro", etapa=t("cancelled"))
    painel.retomar()
    try:
        os.remove(cfg)
    except OSError:
        pass
    for fid, r in resultados.items():
        if not r["ok"] and not r["erro"]:
            r["erro"] = t("conversion_cancelled") if cancelado else t("ended_early")
            r["detalhes"] = "".join(erros_motor[-15:]).strip() or None
    return resultados, cancelado


def confirmar_cancelamento(painel, padrao_sigint):
    """Pausa o painel (para ele não se redesenhar por cima da pergunta) e pergunta se cancela.
    Outro Ctrl+C durante a pergunta confirma o cancelamento."""
    painel.pausar()
    try:
        signal.signal(signal.SIGINT, padrao_sigint)  # durante a pergunta, o Ctrl+C volta a interromper
        print()
        cancelar = ui.sim_nao(t("cancel_q"), padrao=False)
    except KeyboardInterrupt:
        print()
        cancelar = True
    if cancelar:
        ui.dica(t("cancelling"))
    else:
        print()
        painel.retomar()
    return cancelar


def encerrar_motor(proc):
    """Encerra o motor e tudo o que ele abriu (FFmpeg), sem deixar processos para trás."""
    try:
        proc.stdin.close()  # o motor para sozinho quando a entrada fecha
    except Exception:
        pass
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


# ---------------------------------------------------------------------------------------------
# Programa
# ---------------------------------------------------------------------------------------------
def _idioma_pedido(argv):
    """--lang xx / --lang=xx, lido antes do argparse para a própria ajuda (--help) sair no idioma certo."""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def _argumentos():
    parser = argparse.ArgumentParser(description=t("cli_description"))
    parser.add_argument("arquivos", nargs="*", help=t("cli_files_help"))
    parser.add_argument("-o", "--saida", "--output", help=t("cli_output_help"))
    parser.add_argument("--lang", choices=i18n.IDIOMAS, help=t("cli_lang_help", langs=", ".join(i18n.IDIOMAS)))
    parser.add_argument("--voice", help=t("cli_voice_help"))
    parser.add_argument("--format", choices=list(FORMATOS_CLI), help=t("cli_format_help"))
    parser.add_argument("--quality", choices=QUALIDADES, help=t("cli_quality_help"))
    parser.add_argument("--translate", choices=i18n.IDIOMAS, metavar="LANG", help=t("cli_translate_help"))
    sync = parser.add_mutually_exclusive_group()
    sync.add_argument("--sync", dest="texto", action="store_true", default=None, help=t("cli_sync_help"))
    sync.add_argument("--no-sync", dest="texto", action="store_false", help=t("cli_no_sync_help"))
    parser.add_argument("-y", "--yes", action="store_true", help=t("cli_yes_help"))
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--lite", dest="modo", action="store_const", const="lite", help=t("cli_lite_help"))
    modo.add_argument("--turbo", dest="modo", action="store_const", const="turbo", help=t("cli_turbo_help"))
    parser.add_argument("--no-update-check", action="store_true", help=t("cli_no_update_help"))
    parser.add_argument("--version", action="version", version=f"Audiobook Generator (terminal) {VERSAO}")
    return parser.parse_args()


def avisar_atualizacao(verificacao, sem_perguntas):
    """Versão nova no GitHub: mostra o que mudou e oferece atualizar pelo terminal.
    Devolve True quando atualizou (o app fecha para abrir de novo já na versão nova)."""
    release = verificacao.resultado() if verificacao else None
    if not release:
        return False
    nova = str(release.get("tag_name", "")).lstrip("v")
    tipo = atualizacao.tipo_de_instalacao()
    print()
    ui.alerta(t("update_available", new=nova, current=VERSAO))
    itens = atualizacao.traduzir(atualizacao.novidades(release), i18n.atual)
    if itens:
        ui.info(t("update_whats_new"))
        for item in itens:
            linhas = ui.quebrar(item, max(30, ui.largura() - 10))
            print("    " + ui.destaque(ui.ICONE["item"]) + " " + linhas[0])
            for linha in linhas[1:]:
                print("      " + linha)
    ui.dica(t("update_download_page", url=release.get("html_url") or atualizacao.PAGINA))
    depois = t("update_later", command=atualizacao.comando_texto(tipo))
    if sem_perguntas or not sys.stdin.isatty() or not ui.sim_nao(t("update_q"), padrao=True):
        ui.dica(depois)
        return False
    ui.dica(t("updating"))
    resultado = atualizacao.atualizar(release, tipo)
    if resultado == "ok":
        ui.ok(t("update_done", version=nova))
        return True
    if resultado == "janela":
        ui.ok(t("update_windows_window"))
        return True
    ui.falha(t("update_failed"))
    ui.dica("  " + atualizacao.comando_texto(tipo))
    return False


def main():
    i18n.configurar(_idioma_pedido(sys.argv[1:]))
    args = _argumentos()
    # A consulta à última versão começa já, em segundo plano, e é lida depois do banner
    verificacao = None if atualizacao.desligada(args.no_update_check) else atualizacao.Verificacao(VERSAO)
    config = ler_config()
    sem_perguntas = args.yes
    if args.voice and not any(v["c"] == args.voice for v in VOZES):
        ui.falha(t("voice_unknown", voice=args.voice))
        return 1

    ui.banner("Audiobook Generator", VERSAO, t("app_tagline"))
    if avisar_atualizacao(verificacao, sem_perguntas):
        return 0
    tem_ffmpeg = verificar_dependencias()

    # 1) Livros
    ui.etapa(1, TOTAL_ETAPAS, t("step_books"), "livros")
    caminhos = args.arquivos or escolher_arquivos()
    validos = []
    for c in caminhos:
        if not os.path.isfile(c):
            ui.falha(t("file_not_found", path=c))
        elif not c.lower().endswith(EXTENSOES):
            ui.falha(t("unsupported_format", name=os.path.basename(c)))
        else:
            validos.append(c)
    if not validos:
        ui.falha(t("no_ebooks"))
        return 1
    livros = ler_livros(validos)
    if not livros:
        ui.falha(t("none_readable"))
        return 1

    # 2) Informações, tradução, texto sincronizado e voz de cada livro
    ui.etapa(2, TOTAL_ETAPAS, t("step_setup"), "voz")
    configurar_livros(livros, {"sem_perguntas": sem_perguntas, "voz": args.voice, "traduzir": args.translate,
                               "texto": args.texto, "config": config})

    # 3) Formato (sem FFmpeg, só MP3)
    ui.etapa(3, TOTAL_ETAPAS, t("step_format"), "formato")
    sugerido = config.get("formato") if config.get("formato") in (f[0] for f in FORMATOS) else "m4b"
    if not tem_ffmpeg and sugerido not in FORMATOS_SEM_FFMPEG:
        sugerido = "single"
    if args.format:
        formato = FORMATOS_CLI[args.format]
        if not tem_ffmpeg and formato not in FORMATOS_SEM_FFMPEG:
            ui.alerta(t("format_needs_ffmpeg", format=nome_formato(formato)))
            formato = "single"
    elif sem_perguntas:
        formato = sugerido
    else:
        formato = escolher_formato(tem_ffmpeg, sugerido)
    qualidade = args.quality or (config.get("qualidade") if config.get("qualidade") in QUALIDADES else "voice")
    if args.format or sem_perguntas:
        ui.ok(nome_formato(formato))

    # 4) Pasta
    ui.etapa(4, TOTAL_ETAPAS, t("step_folder"), "pasta")
    pasta_salva = config.get("pasta") if config.get("pasta") and os.path.isdir(config["pasta"]) else None
    if args.saida:
        pasta = os.path.normpath(os.path.expanduser(args.saida))
    elif sem_perguntas:
        pasta = pasta_salva or pasta_padrao()
    else:
        pasta = escolher_pasta(pasta_salva)
    os.makedirs(pasta, exist_ok=True)
    ui.ok(t("saving_to", folder=ui.texto(pasta)))

    # 5) Resumo
    ui.etapa(5, TOTAL_ETAPAS, t("step_summary"), "converter")
    for l in livros:
        voz = nome_voz(next(v for v in VOZES if v["c"] == l["voz"]))
        print(f"  {ui.destaque(ui.ICONE['item'])} {ui.negrito(l['titulo'])} {ui.suave('— ' + l['autor'])}")
        detalhes = [t("summary_voice", name=voz[0], details=voz[1]),
                    t("translate_to_lower", language=nome_idioma_frase(l["traduzir"])) if l["traduzir"] else t("without_translation"),
                    t("with_sync") if l["texto"] else t("without_sync")]
        print("    " + ui.suave(" · ".join(detalhes)))
    print("  " + ui.suave(t("summary_format_folder", format=nome_formato(formato), folder=pasta)))
    if not sem_perguntas and not ui.sim_nao(t("start_q"), padrao=True):
        ui.dica(t("not_started"))
        return 0

    # As escolhas viram o padrão da próxima vez (voz por idioma de narração)
    vozes_salvas = dict(config.get("vozes") or {})
    for l in livros:
        vozes_salvas[l["voz"].split("-")[0]] = l["voz"]
    config.update({"vozes": vozes_salvas, "formato": formato, "qualidade": qualidade, "pasta": pasta,
                   "texto": livros[-1]["texto"]})
    salvar_config(config)

    # 6) Conversão
    ui.etapa(6, TOTAL_ETAPAS, t("step_converting"), "converter")
    ui.dica(t("ctrl_c_hint"))
    print()
    inicio = time.time()
    resultados, cancelado = converter(livros, formato, pasta, qualidade, args.modo)

    print()
    concluidos = [l for l in livros if resultados[l["id"]]["ok"]]
    minutos = (time.time() - inicio) / 60
    for l in livros:
        r = resultados[l["id"]]
        if r["ok"]:
            audio = next((a for a in r["arquivos"] if not re.search(r"\.(lrc|srt|vtt)$|\.sync\.json$", a, re.I)), None)
            ui.ok(f"{ui.negrito(l['titulo'])}  {ui.suave(audio or pasta)}")
        else:
            ui.falha(f"{l['titulo']}: {r['erro']}")
            if r["detalhes"]:
                for linha in str(r["detalhes"]).splitlines()[:8]:
                    ui.dica("    " + linha)
    print()
    if concluidos:
        ui.ok(ui.sucesso(t("n_ready", done=len(concluidos), total=len(livros), minutes=f"{minutos:.1f}")))
        if not sem_perguntas and ui.sim_nao(t("open_folder_q"), padrao=True):
            abrir(pasta)
    elif not cancelado:
        ui.falha(t("none_created"))
    return 0 if len(concluidos) == len(livros) else 2


def executar():
    """Ponto de entrada (python audiobook_cli.py e o comando audiobook-generator instalado com pip/pipx)."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        ui.dica(t("exiting"))
        sys.exit(130)


if __name__ == "__main__":
    executar()
