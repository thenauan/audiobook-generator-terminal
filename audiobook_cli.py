"""
Audiobook Generator para o terminal: converte e-books (EPUB, PDF, MOBI, AZW3, TXT) em audiobooks (M4B, MP3...)
com o mesmo motor de conversão do app, sem interface gráfica.

Uso:
    python audiobook_cli.py                       abre o seletor de arquivos
    python audiobook_cli.py livro.epub outro.pdf  já começa com esses livros
    python audiobook_cli.py livro.epub -o "C:\\Audiobooks"
"""
import argparse
import asyncio
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

import ui

VERSAO = "1.2.0"
BASE = os.path.dirname(os.path.abspath(__file__))
MOTOR = os.path.join(BASE, "motor")
sys.path.insert(0, MOTOR)

EXTENSOES = (".epub", ".pdf", ".mobi", ".azw", ".azw3", ".prc", ".txt")
TOTAL_ETAPAS = 6

# Idiomas para os quais dá para traduzir (os da interface do app, todos com vozes)
DESTINOS_TRADUCAO = ["pt", "en", "es", "ar", "ru"]
# Voz sugerida por idioma (a primeira da lista quando o idioma não está aqui)
VOZ_PREFERIDA = {"pt": "pt-BR-AntonioNeural", "en": "en-US-AndrewNeural", "es": "es-ES-AlvaroNeural",
                 "ar": "ar-SA-HamedNeural", "ru": "ru-RU-SvetlanaNeural", "fr": "fr-FR-HenriNeural",
                 "de": "de-DE-ConradNeural", "it": "it-IT-DiegoNeural"}
LOCALE_PREFERIDO = {"pt": "pt-BR", "en": "en-US", "es": "es-ES", "ar": "ar-SA", "zh": "zh-CN", "fr": "fr-FR"}
FRASE_AMOSTRA = {
    "pt": "Olá! Esta é a minha voz. Que tal ouvirmos juntos uma boa história?",
    "en": "Hello! This is my voice. Shall we listen to a good story together?",
    "es": "¡Hola! Esta es mi voz. ¿Escuchamos juntos una buena historia?",
    "ar": "مرحبًا! هذا هو صوتي. هل نستمع معًا إلى قصة جميلة؟",
    "ru": "Привет! Это мой голос. Давайте послушаем хорошую историю вместе.",
}
FORMATOS = [("m4b", "Audiobook M4B", "um arquivo com capítulos e capa — o padrão dos apps de audiobook"),
            ("single", "MP3", "um arquivo com capítulos e capa, toca em qualquer aparelho"),
            ("multiple", "MP3 por capítulo", "uma faixa por capítulo, numa pasta"),
            ("opus", "Opus", "o menor arquivo pela mesma qualidade"),
            ("flac", "FLAC", "sem perda de qualidade (arquivo maior)"),
            ("wav", "WAV", "sem compressão, para edição (sem capítulos nem capa)")]

with open(os.path.join(BASE, "dados", "vozes.json"), encoding="utf-8") as f:
    VOZES = json.load(f)  # [{c: ShortName, l: Locale, g: F|M}]
with open(os.path.join(BASE, "dados", "idiomas.json"), encoding="utf-8") as f:
    _DADOS_IDIOMAS = json.load(f)
IDIOMAS = _DADOS_IDIOMAS["idiomas"]   # código -> {pt, nativo}
REGIOES = _DADOS_IDIOMAS["regioes"]   # BR -> Brasil


# ---------------------------------------------------------------------------------------------
# Idiomas e vozes
# ---------------------------------------------------------------------------------------------
def _sem_acento(t):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c)).lower().strip()


_POR_NOME = {}
for _cod, _n in IDIOMAS.items():
    for _nome in (_n["pt"], _n["nativo"]):
        _POR_NOME.setdefault(_sem_acento(_nome), _cod)


def codigo_idioma(nome):
    """'Português (Brasil)', 'Inglês', 'Grego', 'el', 'sv-SE' -> código de duas letras ('' se não reconhecer)."""
    n = _sem_acento(re.sub(r"\s*\(.*\)$", "", str(nome or "")))
    if not n:
        return ""
    m = re.fullmatch(r"([a-z]{2,3})(?:[-_][a-z0-9]+)*", n)
    if m and m.group(1) in IDIOMAS:
        return m.group(1)
    return _POR_NOME.get(n, "")


def nome_idioma(codigo):
    return IDIOMAS.get(codigo, {}).get("pt", codigo or "desconhecido")


def nome_voz(v):
    nome = v["c"][len(v["l"]) + 1:].replace("Neural", "").replace("Multilingual", " (multilíngue)").strip()
    regiao = next((p for p in v["l"].split("-") if re.fullmatch(r"[A-Z]{2}", p)), "")
    genero = "feminina" if v["g"] == "F" else "masculina"
    onde = REGIOES.get(regiao, regiao)
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
        ui.dica("Abrindo o seletor de arquivos... (ele pode abrir atrás desta janela)")
        caminhos = dialogo.askopenfilenames(
            parent=raiz, title="Escolha os e-books para converter",
            filetypes=[("E-books", " ".join(f"*{e}" for e in EXTENSOES)), ("Todos os arquivos", "*.*")])
        raiz.destroy()
        if caminhos:
            return list(caminhos)
        ui.alerta("Nenhum arquivo escolhido no seletor.")
    ui.dica("Digite ou arraste os arquivos para cá (um ou vários por linha). Linha vazia termina.")
    caminhos = []
    while True:
        linha = ui.perguntar("Arquivo").strip()
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


def escolher_pasta():
    padrao = pasta_padrao()
    raiz, dialogo = _tk()
    if raiz:
        ui.dica("Abrindo o seletor de pastas... (ele pode abrir atrás desta janela)")
        os.makedirs(padrao, exist_ok=True)
        pasta = dialogo.askdirectory(parent=raiz, title="Onde salvar os audiobooks", initialdir=padrao, mustexist=False)
        raiz.destroy()
        if pasta:
            return os.path.normpath(pasta)
        ui.dica(f"Nenhuma pasta escolhida: usando a pasta padrão.")
        return padrao
    pasta = ui.perguntar("Pasta onde salvar", padrao)
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
        ui.alerta(f"Não foi possível abrir: {e}")


# ---------------------------------------------------------------------------------------------
# Verificações
# ---------------------------------------------------------------------------------------------
def verificar_dependencias():
    faltando = []
    for modulo, pacote in [("edge_tts", "edge-tts"), ("bs4", "beautifulsoup4"), ("lxml", "lxml"), ("mutagen", "mutagen"),
                           ("pypdf", "pypdf"), ("pdfplumber", "pdfplumber"), ("PIL", "pillow"), ("mobi", "mobi"), ("langdetect", "langdetect")]:
        try:
            __import__(modulo)
        except Exception:
            faltando.append(pacote)
    if faltando:
        ui.falha("Faltam bibliotecas do Python: " + ", ".join(faltando))
        python = "python" if os.name == "nt" else "python3"
        ui.dica(f"Instale com:  {python} -m pip install -r requirements.txt")
        sys.exit(1)
    if not shutil.which("ffmpeg"):
        # O motor usa o FFmpeg para juntar os capítulos e gerar o arquivo final, em todos os formatos
        ui.falha("FFmpeg não encontrado: ele é necessário para gerar os audiobooks.")
        if os.name == "nt":
            comando = "winget install Gyan.FFmpeg"
        elif sys.platform == "darwin":
            comando = "brew install ffmpeg"
        else:
            comando = "sudo apt install ffmpeg  (ou dnf / pacman, conforme a distribuição)"
        ui.dica(f"Instale com:  {comando}")
        ui.dica("Depois, abra um terminal novo e rode o Audiobook Generator de novo.")
        sys.exit(1)


# ---------------------------------------------------------------------------------------------
# Leitura dos livros
# ---------------------------------------------------------------------------------------------
def ler_livros(caminhos):
    from extract_metadata import extract_metadata
    livros, prontos = [], [0]
    trava = threading.Lock()

    def ler(caminho):
        try:
            dados = extract_metadata(caminho, os.path.basename(caminho))
        except Exception as e:
            dados = {"unreadable": f"não foi possível ler o arquivo ({e})"}
        with trava:
            prontos[0] += 1
            sys.stdout.write(f"\r\033[2K  {ui.destaque(ui.ICONE['info'])} Lendo os livros... {prontos[0]}/{len(caminhos)}")
            sys.stdout.flush()
        return caminho, dados

    with ThreadPoolExecutor(max_workers=min(4, len(caminhos))) as ex:
        resultados = list(ex.map(ler, caminhos))
    sys.stdout.write("\r\033[2K")
    for caminho, d in resultados:
        nome = os.path.basename(caminho)
        if d.get("unreadable"):
            ui.falha(f"{nome}: {d['unreadable']}")
            continue
        texto_inicio = ""
        for cap in d.get("chapters") or []:
            conteudo = (cap.get("content") or "").strip()
            if len(conteudo) > 200:
                texto_inicio = conteudo
                break
        livros.append({
            "id": uuid.uuid4().hex[:10], "caminho": caminho, "arquivo": nome,
            "titulo": d.get("title") or os.path.splitext(nome)[0], "autor": d.get("author") or "Desconhecido",
            "genero": d.get("genre") or "", "subgenero": d.get("subgenre") or "", "ano": d.get("year") or "",
            "idioma": codigo_idioma(d.get("language")), "capitulos": len(d.get("chapters") or []),
            "duracao": d.get("durationEst") or "", "capa": d.get("cover"), "trecho": texto_inicio,
        })
        ui.ok(f"{nome} {ui.suave('· ' + str(len(d.get('chapters') or [])) + ' capítulos')}")
    return livros


# ---------------------------------------------------------------------------------------------
# Amostra de voz (gerada na hora, com um trecho do próprio livro quando dá)
# ---------------------------------------------------------------------------------------------
def tocar_amostra(voz, idioma, trecho=""):
    import edge_tts
    if trecho:
        frase = re.split(r"(?<=[.!?…])\s", re.sub(r"\s+", " ", trecho))[:2]
        frase = " ".join(frase)[:260]
    else:
        frase = FRASE_AMOSTRA.get(idioma, "1, 2, 3.")
    destino = os.path.join(tempfile.gettempdir(), f"amostra_{voz}.mp3")
    ui.dica(f"{ui.ICONE['amostra']} Gerando a amostra de {voz}...")
    try:
        asyncio.run(edge_tts.Communicate(frase, voz).save(destino))
        abrir(destino)
    except Exception as e:
        ui.alerta(f"Não foi possível gerar a amostra ({type(e).__name__}). Verifique a internet.")


# ---------------------------------------------------------------------------------------------
# Escolhas de cada livro
# ---------------------------------------------------------------------------------------------
def revisar_informacoes(livro):
    ui.cartao(livro["titulo"], [
        ("Autor", livro["autor"]), ("Gênero", livro["genero"]), ("Subgênero", livro["subgenero"]),
        ("Ano", livro["ano"]), ("Idioma", nome_idioma(livro["idioma"]) if livro["idioma"] else "não reconhecido"),
        ("Capítulos", str(livro["capitulos"])), ("Duração estimada", livro["duracao"]), ("Arquivo", livro["arquivo"]),
    ])
    if not ui.sim_nao("Editar as informações?", padrao=False):
        return
    livro["titulo"] = ui.perguntar("Título", livro["titulo"])
    livro["autor"] = ui.perguntar("Autor", livro["autor"])
    livro["genero"] = ", ".join(g.strip() for g in ui.perguntar("Gêneros (separe por vírgula)", livro["genero"]).split(",") if g.strip())
    livro["subgenero"] = ", ".join(g.strip() for g in ui.perguntar("Subgêneros (separe por vírgula)", livro["subgenero"]).split(",") if g.strip())
    while True:
        ano = ui.perguntar("Ano", livro["ano"])
        if not ano or re.fullmatch(r"\d{4}", ano):
            livro["ano"] = ano
            break
        ui.alerta("Use 4 dígitos, como 2024 (ou deixe em branco).")
    while True:
        idioma = ui.perguntar("Idioma do livro", nome_idioma(livro["idioma"]) if livro["idioma"] else "")
        codigo = codigo_idioma(idioma)
        if codigo or not idioma:
            livro["idioma"] = codigo
            break
        ui.alerta("Idioma não reconhecido. Digite o nome (ex.: Francês, Grego) ou o código (ex.: fr, el).")
    ui.ok("Informações atualizadas.")


def escolher_traducao(livro):
    if not livro["idioma"]:
        return ""
    destinos = [c for c in DESTINOS_TRADUCAO if c != livro["idioma"]]
    opcoes = [(f"Não traduzir", f"narrar em {nome_idioma(livro['idioma']).lower()}")] + \
             [(f"Traduzir para {nome_idioma(c).lower()}", IDIOMAS[c]["nativo"]) for c in destinos]
    print()
    ui.info(f"{ui.ICONE['traduzir']} Tradução")
    i = ui.escolher("Opção", opcoes, padrao=0)
    return "" if i == 0 else destinos[i - 1]


def escolher_idioma_narracao():
    codigos = sorted(IDIOMAS, key=lambda c: _sem_acento(nome_idioma(c)))
    ui.alerta("O idioma do livro não foi reconhecido. Escolha o idioma da narração:")
    i = ui.escolher("Idioma", [(nome_idioma(c), "") for c in codigos], padrao=codigos.index("pt") if "pt" in codigos else 0, colunas=3)
    return codigos[i]


def escolher_voz(idioma, trecho, sugerida=None):
    vozes = vozes_do_idioma(idioma)
    if not vozes:
        ui.alerta(f"Não há vozes para {nome_idioma(idioma).lower()}. Escolha outro idioma de narração.")
        return escolher_voz(escolher_idioma_narracao(), "", None)
    codigos = [v["c"] for v in vozes]
    padrao = codigos.index(sugerida) if sugerida in codigos else (codigos.index(VOZ_PREFERIDA[idioma]) if VOZ_PREFERIDA.get(idioma) in codigos else 0)
    print()
    ui.info(f"{ui.ICONE['voz']} Voz da narração em {nome_idioma(idioma).lower()} {ui.suave(f'({len(vozes)} vozes)')}")
    opcoes = [nome_voz(v) for v in vozes]
    while True:
        r = ui.escolher("Voz", opcoes, padrao=padrao, extras={"o": "<n>  ouvir a voz n"}, colunas=1 if len(vozes) <= 12 else 2)
        if isinstance(r, tuple):
            n = r[1].strip() or str(padrao + 1)
            if n.isdigit() and 1 <= int(n) <= len(vozes):
                tocar_amostra(codigos[int(n) - 1], idioma, trecho)
                padrao = int(n) - 1
            else:
                ui.alerta(f"Use o + número da voz, por exemplo: o {padrao + 1}")
            continue
        return codigos[r]


def configurar_livros(livros):
    escolhas_por_idioma = {}  # idioma -> (traduzir, texto, voz), quando o usuário quer repetir
    for n, livro in enumerate(livros, 1):
        print()
        ui.info(ui.negrito(f"Livro {n} de {len(livros)}"))
        revisar_informacoes(livro)
        repetir = escolhas_por_idioma.get(livro["idioma"]) if livro["idioma"] else None
        if repetir:
            livro["traduzir"], livro["texto"], livro["voz"] = repetir
            ui.ok(f"Usando as mesmas escolhas dos livros em {nome_idioma(livro['idioma']).lower()}: "
                  f"{('traduzir para ' + nome_idioma(livro['traduzir']).lower()) if livro['traduzir'] else 'sem tradução'}, "
                  f"{'com' if livro['texto'] else 'sem'} texto sincronizado, voz {nome_voz(next(v for v in VOZES if v['c'] == livro['voz']))[0]}")
            continue
        livro["traduzir"] = escolher_traducao(livro)
        print()
        livro["texto"] = ui.sim_nao(f"{ui.ICONE['texto']} Gerar texto sincronizado para audiobook players?", padrao=True)
        idioma_narracao = livro["traduzir"] or livro["idioma"] or escolher_idioma_narracao()
        livro["voz"] = escolher_voz(idioma_narracao, "" if livro["traduzir"] else livro["trecho"])
        restantes = [l for l in livros[n:] if l["idioma"] == livro["idioma"]]
        if livro["idioma"] and restantes and ui.sim_nao(
                f"Usar estas escolhas também nos outros {len(restantes)} livro(s) em {nome_idioma(livro['idioma']).lower()}?", padrao=True):
            escolhas_por_idioma[livro["idioma"]] = (livro["traduzir"], livro["texto"], livro["voz"])


def escolher_formato():
    i = ui.escolher("Formato", [(r, d) for _, r, d in FORMATOS], padrao=0)
    return FORMATOS[i][0], FORMATOS[i][1]


# ---------------------------------------------------------------------------------------------
# Conversão (o mesmo motor do app: converter.py, com os eventos de progresso em JSON)
# ---------------------------------------------------------------------------------------------
NOME_ETAPA = {"translating": "Traduzindo", "editing": "Preparando o texto", "syncing": "Texto sincronizado"}


def converter(livros, formato, pasta):
    config = {
        "outputFolder": pasta, "file_format": formato, "fileType": formato, "audio_quality": "high",
        "sync_text_destination": "both", "sync_text_granularity": "sentences",
        "files": [{
            "id": l["id"], "path": os.path.abspath(l["caminho"]), "filename": l["arquivo"], "title": l["titulo"],
            "author": l["autor"], "genre": l["genero"], "subgenre": l["subgenero"], "year": l["ano"],
            "language": nome_idioma(l["idioma"]) if l["idioma"] else "", "voice": l["voz"],
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
                partes = f"{ev['partsDone']}/{ev['partsTotal']} {ev.get('partsUnit') or 'partes'}" if ev.get("partsTotal") else ""
                etapa = (ev.get("message") or "").rstrip(".…") or NOME_ETAPA.get(st, "Narrando")
                painel.atualizar(fid, pct=float(ev.get("progress") or 0), etapa=etapa, partes=partes)
            elif tipo == "speed":
                bps = ev.get("bytesPerSec") or 0
                painel.atualizar(fid, vel=(f"{bps / 1048576:.1f} MB/s" if bps >= 1048576 else f"{bps // 1024} KB/s") if bps else "")
            elif tipo == "done":
                painel.atualizar(fid, pct=100.0, fim="ok")
                resultados[fid].update(ok=True, arquivos=ev.get("paths") or [])
            elif tipo == "error":
                painel.atualizar(fid, fim="erro", etapa=ev.get("error") or "Falha na conversão")
                resultados[fid].update(erro=ev.get("error"), detalhes=ev.get("details"))
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
                painel.atualizar(fid, fim="erro", etapa="Cancelado")
    painel.retomar()
    try:
        os.remove(cfg)
    except OSError:
        pass
    for fid, r in resultados.items():
        if not r["ok"] and not r["erro"]:
            r["erro"] = "Conversão cancelada." if cancelado else "O processo de conversão terminou antes de concluir este livro."
            r["detalhes"] = "".join(erros_motor[-15:]).strip() or None
    return resultados, cancelado


def confirmar_cancelamento(painel, padrao_sigint):
    """Pausa o painel (para ele não se redesenhar por cima da pergunta) e pergunta se cancela.
    Outro Ctrl+C durante a pergunta confirma o cancelamento."""
    painel.pausar()
    try:
        signal.signal(signal.SIGINT, padrao_sigint)  # durante a pergunta, o Ctrl+C volta a interromper
        print()
        cancelar = ui.sim_nao("Cancelar a conversão? O que já foi narrado fica salvo para continuar depois.", padrao=False)
    except KeyboardInterrupt:
        print()
        cancelar = True
    if cancelar:
        ui.dica("Cancelando...")
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
def main():
    parser = argparse.ArgumentParser(description="Converte e-books em audiobooks no terminal.")
    parser.add_argument("arquivos", nargs="*", help="e-books para converter (EPUB, PDF, MOBI, AZW3, TXT)")
    parser.add_argument("-o", "--saida", help="pasta onde salvar os audiobooks")
    args = parser.parse_args()

    ui.banner("Audiobook Generator", VERSAO, "E-books em audiobooks com vozes neurais · EPUB, PDF, MOBI, AZW3, TXT → M4B, MP3")
    verificar_dependencias()

    # 1) Livros
    ui.etapa(1, TOTAL_ETAPAS, "Escolha os e-books", "livros")
    caminhos = args.arquivos or escolher_arquivos()
    validos = []
    for c in caminhos:
        if not os.path.isfile(c):
            ui.falha(f"Arquivo não encontrado: {c}")
        elif not c.lower().endswith(EXTENSOES):
            ui.falha(f"Formato não suportado: {os.path.basename(c)} (use EPUB, PDF, MOBI, AZW3 ou TXT)")
        else:
            validos.append(c)
    if not validos:
        ui.falha("Nenhum e-book para converter.")
        return 1
    livros = ler_livros(validos)
    if not livros:
        ui.falha("Nenhum dos arquivos pôde ser lido.")
        return 1

    # 2) Informações, tradução, texto sincronizado e voz de cada livro
    ui.etapa(2, TOTAL_ETAPAS, "Informações, tradução e voz", "voz")
    configurar_livros(livros)

    # 3) Formato
    ui.etapa(3, TOTAL_ETAPAS, "Formato dos audiobooks", "formato")
    formato, nome_formato = escolher_formato()

    # 4) Pasta
    ui.etapa(4, TOTAL_ETAPAS, "Onde salvar", "pasta")
    pasta = os.path.normpath(args.saida) if args.saida else escolher_pasta()
    os.makedirs(pasta, exist_ok=True)
    ui.ok(f"Salvando em {ui.texto(pasta)}")

    # 5) Resumo
    ui.etapa(5, TOTAL_ETAPAS, "Resumo", "converter")
    for l in livros:
        voz = nome_voz(next(v for v in VOZES if v["c"] == l["voz"]))
        print(f"  {ui.destaque(ui.ICONE['item'])} {ui.negrito(l['titulo'])} {ui.suave('— ' + l['autor'])}")
        detalhes = [f"voz {voz[0]}, {voz[1]}",
                    f"traduzir para {nome_idioma(l['traduzir']).lower()}" if l["traduzir"] else "sem tradução",
                    "com texto sincronizado" if l["texto"] else "sem texto sincronizado"]
        print("    " + ui.suave(" · ".join(detalhes)))
    print("  " + ui.suave(f"Formato: {nome_formato} · Pasta: {pasta}"))
    if not ui.sim_nao("Começar a conversão?", padrao=True):
        ui.dica("Conversão não iniciada.")
        return 0

    # 6) Conversão
    ui.etapa(6, TOTAL_ETAPAS, "Convertendo", "converter")
    ui.dica("Ctrl+C para cancelar. Se parar no meio, converta de novo: o que já foi narrado é aproveitado.")
    print()
    inicio = time.time()
    resultados, cancelado = converter(livros, formato, pasta)

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
        ui.ok(ui.sucesso(f"{len(concluidos)} de {len(livros)} audiobook(s) pronto(s) em {minutos:.1f} min."))
        if ui.sim_nao("Abrir a pasta dos audiobooks?", padrao=True):
            abrir(pasta)
    elif not cancelado:
        ui.falha("Nenhum audiobook foi gerado.")
    return 0 if len(concluidos) == len(livros) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        ui.dica("Saindo.")
        sys.exit(130)
