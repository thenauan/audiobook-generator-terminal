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

VERSAO = "1.9.0"

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
# Texto sincronizado: o que gerar (frases, palavras ou as duas) e onde guardar
SEPARACOES = [("both", "sync_both", "sync_both_desc"), ("sentences", "sync_sentences", "sync_sentences_desc"),
              ("words", "sync_words", "sync_words_desc")]
DESTINOS_TEXTO = [("both", "where_both", "where_both_desc"), ("metadata", "where_metadata", "where_metadata_desc"),
                  ("file", "where_file", "where_file_desc")]
# Formatos com lugar padrão para o texto dentro do arquivo. No WAV não existe (só um bloco ID3 que poucos leem)
# e no MP3 por capítulo o livro não é um arquivo só: nesses, o texto vai sempre em arquivo separado
FORMATOS_COM_TEXTO = ("m4b", "single", "opus", "flac")

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


TECLA_SELETOR = "o"  # reabre o seletor de arquivos/pasta depois de fechá-lo sem escolher nada
TECLA_IDIOMA = "l"  # na pergunta dos arquivos, troca o idioma da interface


def escolher_idioma_interface(config):
    """Troca o idioma da interface e guarda a escolha; "idioma do sistema" apaga a escolha guardada."""
    codigos = list(i18n.IDIOMAS)
    opcoes = [(t("language_system"), IDIOMAS[i18n.idioma_do_sistema()]["nativo"])] + [(IDIOMAS[c]["nativo"], "") for c in codigos]
    salvo = config.get("idioma")
    i = ui.escolher(t("prompt_interface_language"), opcoes, padrao=codigos.index(salvo) + 1 if salvo in codigos else 0)
    if i:
        config["idioma"] = codigos[i - 1]
    else:
        config.pop("idioma", None)
    salvar_config(config)
    i18n.configurar(config.get("idioma"))
    ui.ok(t("language_changed", language=IDIOMAS[i18n.atual]["nativo"]))


def escolher_arquivos(config):
    """Seletor do sistema; se ele for fechado sem escolher nada, dá para abri-lo de novo (Enter ou o)
    ou digitar/arrastar os arquivos. Sem seletor (sem tkinter, SSH), só digitando."""
    while True:
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
            ui.dica(t("files_hint_picker"))
        else:
            ui.dica(t("type_or_drag"))
        ui.dica(t("files_hint_language", key=TECLA_IDIOMA))
        caminhos = []
        while True:
            linha = ui.perguntar(t("prompt_file")).strip()
            if raiz and linha.lower() == TECLA_SELETOR:
                break  # abre o seletor de novo
            if linha.lower() == TECLA_IDIOMA and not caminhos:
                escolher_idioma_interface(config)
                break  # volta à escolha dos arquivos, já no idioma novo
            if not linha:
                if caminhos:
                    return caminhos
                if raiz:
                    break  # nada digitado ainda: Enter abre o seletor de novo
                continue
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
    """Seletor de pastas; se ele for fechado sem escolher nada: o abre de novo, Enter usa a pasta padrão,
    ou dá para digitar/arrastar uma pasta."""
    padrao = sugerida or pasta_padrao()
    while True:
        raiz, dialogo = _tk()
        if not raiz:
            pasta = ui.perguntar(t("prompt_folder"), padrao)
            return os.path.normpath((caminhos_digitados(pasta) or [padrao])[0])
        ui.dica(t("opening_folder_picker"))
        os.makedirs(padrao, exist_ok=True)
        pasta = dialogo.askdirectory(parent=raiz, title=t("folder_picker_title"), initialdir=padrao, mustexist=False)
        raiz.destroy()
        if pasta:
            return os.path.normpath(pasta)
        ui.alerta(t("no_folder_picked"))
        ui.dica(t("folder_hint_picker"))
        resposta = ui.perguntar(t("prompt_folder"), padrao).strip()
        if resposta.lower() == TECLA_SELETOR:
            continue  # abre o seletor de novo
        return os.path.normpath((caminhos_digitados(resposta) or [padrao])[0])


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
# Recursos do computador (para ligar o modo leve sozinho em computadores mais fracos)
# ---------------------------------------------------------------------------------------------
def memoria_gb():
    """Memória RAM total em GB (None se não der para saber)."""
    try:
        if os.name == "nt":
            import ctypes

            class Estado(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            estado = Estado()
            estado.dwLength = ctypes.sizeof(Estado)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(estado)):
                return estado.ullTotalPhys / 1024 ** 3
            return None
        if sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3)
            return int(r.stdout.strip()) / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except Exception:
        return None


def na_bateria():
    """True se o notebook está fora da tomada, False se está na tomada, None se não der para saber (ou desktop)."""
    try:
        if os.name == "nt":
            import ctypes

            class Energia(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

            energia = Energia()
            if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(energia)) or energia.BatteryFlag == 128:
                return None  # 128: sem bateria (desktop)
            return {0: True, 1: False}.get(energia.ACLineStatus)
        if sys.platform == "darwin":
            r = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3)
            return "Battery Power" in r.stdout if "InternalBattery" in r.stdout else None
        base = "/sys/class/power_supply"
        fontes = os.listdir(base) if os.path.isdir(base) else []
        if not any(open(os.path.join(base, f, "type")).read().strip() == "Battery" for f in fontes
                   if os.path.exists(os.path.join(base, f, "type"))):
            return None
        tomadas = [os.path.join(base, f, "online") for f in fontes
                   if os.path.exists(os.path.join(base, f, "type"))
                   and open(os.path.join(base, f, "type")).read().strip() == "Mains"
                   and os.path.exists(os.path.join(base, f, "online"))]
        return all(open(p).read().strip() == "0" for p in tomadas) if tomadas else None
    except Exception:
        return None


def computador_mais_fraco(nucleos, ram, bateria):
    """Modo leve automático: pelo menos dois sinais (até 4 núcleos, menos de 8 GB, fora da tomada)
    ou um sinal forte (até 2 núcleos ou menos de 4 GB)."""
    if nucleos <= 2 or (ram is not None and ram < 4):
        return True
    return sum([nucleos <= 4, ram is not None and ram < 8, bateria is True]) >= 2


def avisar_modo_leve(motivo):
    """Avisa que o modo leve está ligado, por quê, e o que muda em relação ao modo adaptável."""
    print()
    ui.alerta(motivo)
    ui.info(t("lite_what_changes"))
    for chave in ("lite_change_books", "lite_change_voice", "lite_change_cpu", "lite_change_result"):
        print("    " + ui.destaque(ui.ICONE["item"]) + " " + t(chave))
    ui.dica(t("lite_how_off"))


def decidir_modo_leve(pedido, config):
    """--lite / --no-lite (guardado para as próximas vezes); senão a escolha guardada; senão pelo computador:
    o modo adaptável é o padrão, e o leve só liga sozinho num computador mais fraco. Devolve se o leve fica ligado."""
    if pedido is not None:
        config["leve"] = pedido
    if isinstance(config.get("leve"), bool):
        if config["leve"]:
            avisar_modo_leve(t("lite_on_chosen"))
        return config["leve"]
    nucleos, ram, bateria = os.cpu_count() or 2, memoria_gb(), na_bateria()
    if not computador_mais_fraco(nucleos, ram, bateria):
        return False
    avisar_modo_leve(t("lite_on_auto", cores=nucleos, ram=f"{ram:.0f}" if ram is not None else "?",
                       battery=t("battery_suffix") if bateria else ""))
    return True


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


def escolher_separacao(sugerida):
    padrao = next((i for i, s in enumerate(SEPARACOES) if s[0] == sugerida), 0)
    print()
    ui.info(f"{ui.ICONE['texto']} " + t("sync_detail_q"))
    return SEPARACOES[ui.escolher(t("prompt_option"), [(t(n), t(d)) for _, n, d in SEPARACOES], padrao=padrao)][0]


def escolher_destino_texto(sugerido):
    padrao = next((i for i, d in enumerate(DESTINOS_TEXTO) if d[0] == sugerido), 0)
    print()
    ui.info(f"{ui.ICONE['texto']} " + t("sync_where_q"))
    return DESTINOS_TEXTO[ui.escolher(t("prompt_option"), [(t(n), t(d)) for _, n, d in DESTINOS_TEXTO], padrao=padrao)][0]


def configurar_livros(livros, opcoes):
    """Informações, tradução, texto sincronizado e voz de cada livro. `opcoes`: o que veio da linha de comando
    (sem_perguntas, voz, traduzir, texto, separacao) e as últimas escolhas salvas (config)."""
    config = opcoes["config"]
    salva = config.get("separacao") if config.get("separacao") in (s[0] for s in SEPARACOES) else "both"
    separacao_padrao = opcoes["separacao"] or salva
    voz_pedida = opcoes["voz"]
    idioma_voz_pedida = next((v["l"].split("-")[0] for v in VOZES if v["c"] == voz_pedida), None)
    texto_padrao = config.get("texto", True) if opcoes["texto"] is None else opcoes["texto"]
    escolhas_por_idioma = {}  # idioma -> (traduzir, texto, separacao, voz), quando o usuário quer repetir
    for n, livro in enumerate(livros, 1):
        if opcoes["sem_perguntas"]:
            destino = opcoes["traduzir"]
            livro["traduzir"] = destino if destino and destino != livro["idioma"] else ""
            livro["texto"] = texto_padrao
            livro["separacao"] = separacao_padrao
            idioma_narracao = livro["traduzir"] or livro["idioma"] or idioma_voz_pedida or i18n.atual
            livro["voz"] = voz_automatica(idioma_narracao, voz_pedida, config)
            continue
        print()
        ui.info(ui.negrito(t("book_n_of", n=n, total=len(livros))))
        revisar_informacoes(livro)
        repetir = escolhas_por_idioma.get(livro["idioma"]) if livro["idioma"] else None
        if repetir:
            livro["traduzir"], livro["texto"], livro["separacao"], livro["voz"] = repetir
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
        livro["separacao"] = separacao_padrao
        if livro["texto"] and not opcoes["separacao"]:
            livro["separacao"] = escolher_separacao(separacao_padrao)
        idioma_narracao = livro["traduzir"] or livro["idioma"] or idioma_voz_pedida or escolher_idioma_narracao()
        if voz_pedida:
            livro["voz"] = voz_automatica(idioma_narracao, voz_pedida, config)
        else:
            livro["voz"] = escolher_voz(idioma_narracao, "" if livro["traduzir"] else livro["trecho"], voz_padrao(idioma_narracao, config))
        restantes = [l for l in livros[n:] if l["idioma"] == livro["idioma"]]
        if livro["idioma"] and restantes and ui.sim_nao(
                t("reuse_choices_q", n=len(restantes), language=nome_idioma_frase(livro["idioma"])), padrao=True):
            escolhas_por_idioma[livro["idioma"]] = (livro["traduzir"], livro["texto"], livro["separacao"], livro["voz"])


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

_ANCESTRAIS = None


def _processos_ancestrais():
    """Processos que abriram este (o shell, o Windows Terminal, o VS Code...), lidos uma vez."""
    global _ANCESTRAIS
    if _ANCESTRAIS is not None:
        return _ANCESTRAIS
    import ctypes
    from ctypes import wintypes

    class Entrada(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    foto = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    pais = {}
    try:
        e = Entrada()
        e.dwSize = ctypes.sizeof(Entrada)
        ok = k32.Process32FirstW(foto, ctypes.byref(e))
        while ok:
            pais[e.th32ProcessID] = e.th32ParentProcessID
            ok = k32.Process32NextW(foto, ctypes.byref(e))
    finally:
        k32.CloseHandle(foto)
    vistos, pid = set(), os.getpid()
    while pid and pid not in vistos:
        vistos.add(pid)
        pid = pais.get(pid)
    _ANCESTRAIS = vistos
    return vistos


def janela_em_primeiro_plano():
    """Se a janela deste terminal está em primeiro plano (não minimizada nem atrás de outra). Só no Windows;
    None quando não dá para saber (macOS, Linux), e aí nada muda na conversão."""
    if os.name != "nt" or not sys.stdin.isatty():
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.GetForegroundWindow.restype = wintypes.HWND
        k32.GetConsoleWindow.restype = wintypes.HWND
        u32.GetAncestor.restype = wintypes.HWND
        u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        ativa, console = u32.GetForegroundWindow(), k32.GetConsoleWindow()
        if not ativa:
            return False
        # Console clássico: a própria janela; Windows Terminal: a janela dona do console
        if console and (ativa == console or ativa == u32.GetAncestor(console, 3)):  # GA_ROOTOWNER
            return True
        dono = wintypes.DWORD()
        u32.GetWindowThreadProcessId(ativa, ctypes.byref(dono))
        return dono.value in _processos_ancestrais()  # terminal do VS Code, Windows Terminal que abriu o shell...
    except Exception:
        return None


def converter(livros, formato, pasta, qualidade="voice", leve=False, destino_texto="both"):
    """Modo adaptável (conexões de voz e processador se ajustam sozinhos) ou, em computadores mais fracos,
    modo leve (um livro por vez, 4 conexões, uma etapa de áudio por vez e prioridade mínima)."""
    destino = {"both": "both", "metadata": "metadata", "file": "library"}[destino_texto]  # nomes do motor
    config = {
        "outputFolder": pasta, "file_format": formato, "fileType": formato, "audio_quality": qualidade,
        # Texto sincronizado com as frases e, dentro de cada frase, as palavras (cada uma com seu tempo)
        "sync_text_destination": destino,
        "lite": leve, "adaptive": not leve,
        "files": [{
            "id": l["id"], "path": os.path.abspath(l["caminho"]), "filename": l["arquivo"], "title": l["titulo"],
            "cache_id": l["cache_id"],
            "author": l["autor"], "genre": l["genero"], "subgenre": l["subgenero"], "year": l["ano"],
            # O motor recebe o nome do idioma em português, como no app
            "language": IDIOMAS.get(l["idioma"], {}).get("pt", "") if l["idioma"] else "", "voice": l["voz"],
            "translate": bool(l["traduzir"]), "fileType": formato, "file_format": formato, "cover": l["capa"],
            "generic_cover": True,  # e-book sem capa: o motor gera a capa genérica do app
            "narrator": nome_voz(next(v for v in VOZES if v["c"] == l["voz"]))[0],
            "chapters": [], "generate_sync_text": l["texto"], "sync_text_destination": destino,
            # Frases, palavras ou as duas (cada uma com seu tempo)
            "sync_text_granularity": {"both": "both", "sentences": "sentences", "words": "words"}[l["separacao"]],
        } for l in livros],
    }
    # Janela em primeiro plano: a conversão tem prioridade; minimizada, fica mais leve (só onde dá para saber)
    plano = None if leve else janela_em_primeiro_plano()
    if plano is not None:
        config["foreground"] = plano
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
                vel = (f"{bps / 1048576:.1f} MB/s" if bps >= 1048576 else f"{bps // 1024} KB/s") if bps else ""
                if vel and ev.get("connections"):  # quantas conexões o ajuste automático está usando agora
                    vel += " · " + t("n_connections", n=ev["connections"])
                painel.atualizar(fid, vel=vel)
            elif tipo == "done":
                painel.atualizar(fid, pct=100.0, fim="ok")
                resultados[fid].update(ok=True, arquivos=ev.get("paths") or [], erro_texto=ev.get("syncTextError"))
            elif tipo == "error":
                erro = i18n.do_motor(ev.get("error"), ev.get("code"), ev.get("args")) or t("conversion_failed")
                painel.atualizar(fid, fim="erro", etapa=erro)
                resultados[fid].update(erro=erro, detalhes=ev.get("details"), parte=ev.get("failedPart"))
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
    ultima_olhada = time.time()
    try:
        while leitor.is_alive():
            leitor.join(0.3)
            if plano is not None and time.time() - ultima_olhada >= 2:  # a janela mudou de plano: avisa o motor
                ultima_olhada = time.time()
                agora = janela_em_primeiro_plano()
                if agora is not None and agora != plano:
                    plano = agora
                    try:
                        proc.stdin.write(json.dumps({"foreground": plano}) + "\n")
                        proc.stdin.flush()
                    except (OSError, ValueError):
                        pass
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
            r["erro"] = t("conversion_cancelled") if cancelado else motivo_da_queda(erros_motor, proc.returncode)
            r["detalhes"] = "".join(erros_motor[-40:]).strip() or None
    return resultados, cancelado


def motivo_da_queda(linhas, codigo):
    """Por que o motor parou sem dizer nada, a partir das últimas linhas de erro dele."""
    texto = "".join(linhas)
    falta = re.search(r"No module named '([^']+)'", texto)
    if falta:
        return t("err_missing_lib", falta.group(1).replace("_", "-"))
    if "MemoryError" in texto:
        return t("err_memory")
    if re.search(r"No space left|ENOSPC", texto):
        return t("err_disk_full")
    ultima = next((l.strip() for l in reversed(linhas) if l.strip()), "")
    return f"{t('ended_early')} ({ultima[:160] or codigo})"


def salvar_detalhes_falhas(falhas):
    """Guarda os detalhes técnicos das falhas num arquivo (no terminal fica só o motivo). Devolve o caminho."""
    texto = "\n\n".join(f"{titulo}\n{erro}\n\n{detalhes}" for titulo, erro, detalhes in falhas if detalhes)
    if not texto:
        return None
    caminho = os.path.join(os.path.dirname(caminho_config()), "last-error.log")
    try:
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(texto + "\n")
        return caminho
    except OSError:
        return None


def escolher_apos_falha(falhas, resultados, config):
    """Pergunta o que fazer com os livros que falharam. Devolve se é para converter de novo."""
    # Sem saber o idioma do livro, não dá para escolher uma voz que leia o texto original
    sem_traducao = [l for l in falhas if resultados[l["id"]].get("parte") == "translation" and l["traduzir"] and l["idioma"]]
    acoes = [("retry", t("retry_option"), t("retry_desc"))]
    if sem_traducao:
        acoes.append(("skip", t("skip_translation_option"), t("skip_translation_desc")))
    acoes.append(("cancel", t("cancel_option"), t("cancel_desc")))
    print()
    acao = acoes[ui.escolher(t("failed_what_now"), [(a[1], a[2]) for a in acoes])][0]
    if acao == "skip":
        for l in sem_traducao:
            l["traduzir"] = ""
            l["voz"] = voz_automatica(l["idioma"], None, config)  # a voz era do idioma da tradução
            ui.dica(t("skip_translation_voice", title=l["titulo"], voice=nome_voz(next(v for v in VOZES if v["c"] == l["voz"]))[0]))
    return acao != "cancel"


def metadados_gravados(caminho):
    """O que está de fato gravado no arquivo final, lido de volta dele."""
    import mutagen
    audio = mutagen.File(caminho)
    tags = audio.tags if audio is not None and audio.tags else {}
    chaves = set(tags.keys())
    ext = os.path.splitext(caminho)[1].lower()
    if ext in (".mp3", ".wav"):
        def tem(prefixo):
            return any(k.startswith(prefixo) for k in chaves)
        return {"title": tem("TIT2"), "author": tem("TPE1"), "genre": tem("TCON"), "year": tem("TDRC"), "cover": tem("APIC"),
                "chapters": tem("CHAP"), "text": tem("USLT") or tem("SYLT"), "narrator": tem("TCOM")}
    if ext in (".m4b", ".m4a"):
        return {"title": "\xa9nam" in chaves, "author": "\xa9ART" in chaves, "genre": "\xa9gen" in chaves, "year": "\xa9day" in chaves,
                "cover": "covr" in chaves, "chapters": bool(getattr(audio, "chapters", None)), "text": "\xa9lyr" in chaves,
                "narrator": "\xa9wrt" in chaves}
    chaves = {k.lower() for k in chaves}  # FLAC e Opus
    return {"title": "title" in chaves, "author": "artist" in chaves, "genre": "genre" in chaves, "year": "date" in chaves,
            "cover": bool(getattr(audio, "pictures", None)) or "metadata_block_picture" in chaves,
            "chapters": "chapter001" in chaves, "text": "lyrics" in chaves, "narrator": "narrator" in chaves}


def conferir_metadados(arquivo, livro, formato, destino_texto):
    """Lê o arquivo pronto e mostra o que entrou nele; se faltar algo que deveria estar lá, avisa."""
    try:
        gravado = metadados_gravados(arquivo)
    except Exception:
        return
    esperado = ["title", "author", "narrator", "cover"]
    esperado += ["genre"] if livro["genero"] else []
    esperado += ["year"] if livro["ano"] else []
    esperado += ["chapters"] if formato != "multiple" and livro["capitulos"] > 1 else []
    if livro["texto"] and formato in FORMATOS_COM_TEXTO and destino_texto != "file":
        esperado.append("text")
    ok = [t("meta_" + c) for c in esperado if gravado.get(c)]
    faltando = [t("meta_" + c) for c in esperado if not gravado.get(c)]
    if ok:
        ui.dica("    " + t("meta_saved", items=", ".join(ok)))
    if faltando:
        ui.alerta(t("meta_missing", items=", ".join(faltando)))


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


def _idioma_valido(valor):
    if valor not in i18n.IDIOMAS:
        raise argparse.ArgumentTypeError(", ".join(i18n.IDIOMAS))
    return valor


def _argumentos():
    parser = argparse.ArgumentParser(description=t("cli_description"))
    parser.add_argument("arquivos", nargs="*", help=t("cli_files_help"))
    parser.add_argument("-o", "--saida", "--output", help=t("cli_output_help"))
    parser.add_argument("--lang", nargs="?", const=True, type=_idioma_valido, metavar="LANG", help=t("cli_lang_help", langs=", ".join(i18n.IDIOMAS)))
    parser.add_argument("--voice", help=t("cli_voice_help"))
    parser.add_argument("--format", choices=list(FORMATOS_CLI), help=t("cli_format_help"))
    parser.add_argument("--quality", choices=QUALIDADES, help=t("cli_quality_help"))
    parser.add_argument("--translate", choices=i18n.IDIOMAS, metavar="LANG", help=t("cli_translate_help"))
    sync = parser.add_mutually_exclusive_group()
    sync.add_argument("--sync", dest="texto", action="store_true", default=None, help=t("cli_sync_help"))
    sync.add_argument("--no-sync", dest="texto", action="store_false", help=t("cli_no_sync_help"))
    parser.add_argument("--sync-detail", choices=[s[0] for s in SEPARACOES], help=t("cli_sync_detail_help"))
    parser.add_argument("--sync-save", choices=[d[0] for d in DESTINOS_TEXTO], help=t("cli_sync_save_help"))
    parser.add_argument("-y", "--yes", action="store_true", help=t("cli_yes_help"))
    leve = parser.add_mutually_exclusive_group()
    leve.add_argument("--lite", dest="leve", action="store_true", default=None, help=t("cli_lite_help"))
    leve.add_argument("--no-lite", dest="leve", action="store_false", help=t("cli_no_lite_help"))
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
    config = ler_config()
    i18n.configurar(_idioma_pedido(sys.argv[1:]), config.get("idioma"))
    args = _argumentos()
    # A consulta à última versão começa já, em segundo plano, e é lida depois do banner
    verificacao = None if atualizacao.desligada(args.no_update_check) else atualizacao.Verificacao(VERSAO)
    sem_perguntas = args.yes
    if args.voice and not any(v["c"] == args.voice for v in VOZES):
        ui.falha(t("voice_unknown", voice=args.voice))
        return 1

    ui.banner("Audiobook Generator", VERSAO, t("app_tagline"))
    if avisar_atualizacao(verificacao, sem_perguntas):
        return 0
    if not sem_perguntas and sys.stdin.isatty():
        if args.lang is True:  # --lang sem valor: abre a lista
            escolher_idioma_interface(config)
        elif not args.lang and not config.get("idioma"):
            ui.dica(t("language_hint", language=IDIOMAS[i18n.atual]["nativo"], key=TECLA_IDIOMA))
    tem_ffmpeg = verificar_dependencias()
    modo_leve = decidir_modo_leve(args.leve, config)
    if args.leve is not None:
        salvar_config(config)  # --lite / --no-lite vale também para as próximas vezes

    # 1) Livros
    ui.etapa(1, TOTAL_ETAPAS, t("step_books"), "livros")
    caminhos = args.arquivos or escolher_arquivos(config)
    while True:
        validos = []
        for c in caminhos:
            if not os.path.isfile(c):
                ui.falha(t("file_not_found", path=c))
            elif not c.lower().endswith(EXTENSOES):
                ui.falha(t("unsupported_format", name=os.path.basename(c)))
            else:
                validos.append(c)
        if validos:
            break
        ui.falha(t("no_ebooks"))
        if args.arquivos or sem_perguntas or not sys.stdin.isatty():
            return 1
        caminhos = escolher_arquivos(config)  # nada válido escolhido: escolhe de novo
    livros = ler_livros(validos)
    if not livros:
        ui.falha(t("none_readable"))
        return 1

    # 2) Informações, tradução, texto sincronizado e voz de cada livro
    ui.etapa(2, TOTAL_ETAPAS, t("step_setup"), "voz")
    configurar_livros(livros, {"sem_perguntas": sem_perguntas, "voz": args.voice, "traduzir": args.translate,
                               "texto": args.texto, "separacao": args.sync_detail, "config": config})

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

    # Onde guardar o texto sincronizado (depende do formato: nem todo arquivo tem lugar para ele)
    destino_texto = "both"
    if any(l["texto"] for l in livros):
        salvo = config.get("destino_texto") if config.get("destino_texto") in (d[0] for d in DESTINOS_TEXTO) else "both"
        if formato not in FORMATOS_COM_TEXTO:
            destino_texto = "file"
            ui.dica(t("sync_file_only", format=nome_formato(formato)))
        elif args.sync_save:
            destino_texto = args.sync_save
        elif sem_perguntas:
            destino_texto = salvo
        else:
            destino_texto = escolher_destino_texto(salvo)

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
                    t("with_sync") + f" ({t(next(n for c, n, _ in SEPARACOES if c == l['separacao'])).lower()}; "
                    f"{t(next(n for c, n, _ in DESTINOS_TEXTO if c == destino_texto)).lower()})" if l["texto"] else t("without_sync")]
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
                   "texto": livros[-1]["texto"], "separacao": livros[-1]["separacao"]})
    if any(l["texto"] for l in livros) and formato in FORMATOS_COM_TEXTO:
        config["destino_texto"] = destino_texto
    salvar_config(config)

    # 6) Conversão
    ui.etapa(6, TOTAL_ETAPAS, t("step_converting"), "converter")
    ui.dica(t("ctrl_c_hint"))
    if not modo_leve and janela_em_primeiro_plano() is not None:
        ui.dica(t("foreground_hint"))
    print()
    inicio = time.time()
    concluidos, pendentes = [], livros
    while True:
        resultados, cancelado = converter(pendentes, formato, pasta, qualidade, modo_leve, destino_texto)
        print()
        falhas = []
        for l in pendentes:
            r = resultados[l["id"]]
            if r["ok"]:
                concluidos.append(l)
                audio = next((a for a in r["arquivos"] if not re.search(r"\.(lrc|srt|vtt)$|\.sync\.json$", a, re.I)), None)
                ui.ok(f"{ui.negrito(l['titulo'])}  {ui.suave(audio or pasta)}")
                if audio:
                    conferir_metadados(audio, l, formato, destino_texto)
                if r.get("erro_texto"):
                    ui.alerta(t("sync_failed", r["erro_texto"]))
            else:
                falhas.append(l)
                ui.falha(f"{l['titulo']}: {r['erro']}")
        registro = salvar_detalhes_falhas([(l["titulo"], resultados[l["id"]]["erro"], resultados[l["id"]]["detalhes"]) for l in falhas])
        if registro:
            ui.dica(t("error_details_saved", path=registro))
        if not falhas or cancelado or sem_perguntas or not sys.stdin.isatty():
            break
        if not escolher_apos_falha(falhas, resultados, config):
            break
        pendentes = falhas
        print()
    minutos = (time.time() - inicio) / 60
    if concluidos and formato == "wav":
        ui.dica(t("wav_limits"))
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
