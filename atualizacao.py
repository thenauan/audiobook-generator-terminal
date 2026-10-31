"""
Aviso de versão nova: ao abrir, consulta a última release no GitHub (em segundo plano, sem atrasar o app;
sem internet, não mostra nada), mostra o que há de novo e atualiza pelo próprio terminal, do jeito certo
para cada instalação:
    pipx            pipx upgrade audiobook-generator-terminal
    pip             python -m pip install -U git+https://github.com/...
    clone do git    git pull --ff-only
    .zip / .tar.gz  baixa o pacote da release e copia os arquivos por cima desta pasta
Desligar: --no-update-check ou AUDIOBOOK_NO_UPDATE_CHECK=1.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import zipfile

REPO = "thenauan/audiobook-generator-terminal"
PAGINA = f"https://github.com/{REPO}/releases/latest"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PACOTE = "audiobook-generator-terminal"
GIT_URL = f"git+https://github.com/{REPO}.git"
BASE = os.path.dirname(os.path.abspath(__file__))


def _versao(texto):
    """'v1.4.0' -> (1, 4, 0)."""
    return tuple(int(n) for n in re.findall(r"\d+", str(texto))[:3]) or (0,)


def _baixar(url, tempo=10):
    pedido = urllib.request.Request(url, headers={"User-Agent": PACOTE, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(pedido, timeout=tempo) as r:
        return r.read()


class Verificacao:
    """Consulta a última release numa thread; `resultado(espera)` devolve a release se for mais nova."""

    def __init__(self, versao_atual):
        self.versao_atual = versao_atual
        self._release = None
        self._thread = threading.Thread(target=self._consultar, daemon=True)
        self._thread.start()

    def _consultar(self):
        try:
            self._release = json.loads(_baixar(API, tempo=4))
        except Exception:
            self._release = None

    def resultado(self, espera=2.5):
        self._thread.join(espera)
        r = self._release
        if not r or r.get("draft") or r.get("prerelease"):
            return None
        return r if _versao(r.get("tag_name")) > _versao(self.versao_atual) else None


def desligada(args_desligado):
    return args_desligado or os.environ.get("AUDIOBOOK_NO_UPDATE_CHECK") not in (None, "", "0")


# ---------------------------------------------------------------------------------------------
# O que há de novo
# ---------------------------------------------------------------------------------------------
def novidades(release, limite=8):
    """Itens da seção de novidades das notas da release ("What's new"; senão, a primeira lista), sem Markdown."""
    secoes = [("", [])]  # (título, itens)
    for linha in (release.get("body") or "").splitlines():
        if linha.startswith("#"):
            secoes.append((linha, []))
            continue
        m = re.match(r"\s*[-*]\s+(.*)", linha)
        if m:
            texto = re.sub(r"\*\*|`|\[([^\]]+)\]\([^)]+\)", lambda x: x.group(1) or "", m.group(1)).strip()
            if texto:
                secoes[-1][1].append(texto)
    com_itens = [s for s in secoes if s[1]]
    escolhida = next((s for s in com_itens if re.search(r"new|novo|novidade", s[0], re.I)), com_itens[0] if com_itens else ("", []))
    return escolhida[1][:limite]


def _traduzir_texto(texto, alvo):
    """Pelo endpoint público do Google Tradutor (o mesmo que o motor usa); se ele recusar, pelo deep_translator."""
    try:
        import urllib.parse
        corpo = urllib.parse.urlencode({"q": texto}).encode("utf-8")
        url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
            {"client": "gtx", "sl": "en", "tl": alvo, "dt": "t", "ie": "UTF-8", "oe": "UTF-8"})
        pedido = urllib.request.Request(url, data=corpo, headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(pedido, timeout=6) as r:
            dados = json.loads(r.read().decode("utf-8"))
        traduzido = "".join(seg[0] for seg in (dados[0] or []) if seg and seg[0])
        if traduzido.strip():
            return traduzido
    except Exception:
        pass
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="en", target=alvo).translate(texto)


def traduzir(itens, idioma):
    """As notas são escritas em inglês: traduz para o idioma da interface quando dá (sem internet, ficam em inglês)."""
    if idioma == "en" or not itens:
        return itens
    try:
        traduzido = _traduzir_texto("\n".join(itens), {"zh": "zh-CN"}.get(idioma, idioma))
        linhas = [l.strip() for l in (traduzido or "").split("\n") if l.strip()]
        return linhas if len(linhas) == len(itens) else itens
    except Exception:
        return itens


# ---------------------------------------------------------------------------------------------
# Como atualizar
# ---------------------------------------------------------------------------------------------
def tipo_de_instalacao():
    if os.path.isdir(os.path.join(BASE, ".git")) and shutil.which("git"):
        return "git"
    if "site-packages" in BASE or "dist-packages" in BASE:
        prefixo = os.path.normcase(sys.prefix)
        return "pipx" if "pipx" in prefixo and shutil.which("pipx") else "pip"
    return "pacote"


def comando(tipo):
    """O comando que atualiza (para mostrar e para rodar); no tipo "pacote", None (é feito aqui mesmo)."""
    if tipo == "pipx":
        return ["pipx", "upgrade", PACOTE]
    if tipo == "pip":
        return [sys.executable, "-m", "pip", "install", "-U", GIT_URL]
    if tipo == "git":
        return ["git", "-C", BASE, "pull", "--ff-only"]
    return None


def comando_texto(tipo):
    cmd = comando(tipo)
    if not cmd:
        return PAGINA
    if tipo == "pip":
        cmd = ["python" if os.name == "nt" else "python3"] + cmd[1:]
    if tipo == "git":
        cmd = ["git", "pull"]
    return " ".join(cmd)


def atualizar(release, tipo):
    """Atualiza. Devolve "ok", "janela" (Windows: continua numa janela nova depois que o app fechar) ou "erro"."""
    cmd = comando(tipo)
    if tipo in ("pip", "pipx") and os.name == "nt":
        # No Windows, o executável do app está em uso enquanto ele roda: o pip/pipx não conseguiria trocá-lo.
        # A atualização roda numa janela nova, depois de 3 s (tempo de este app fechar).
        linha = "timeout /t 3 /nobreak >nul & " + subprocess.list2cmdline(cmd) + " & pause"
        subprocess.Popen(["cmd", "/c", linha], creationflags=subprocess.CREATE_NEW_CONSOLE)
        return "janela"
    if cmd:
        try:
            return "ok" if subprocess.run(cmd).returncode == 0 else "erro"
        except OSError:
            return "erro"
    return _atualizar_pacote(release)


def _atualizar_pacote(release):
    """Instalação pelo .zip/.tar.gz: baixa o pacote da release e copia os arquivos por cima desta pasta.
    Arquivos que só existem aqui ficam; escolhas e livros não ficam nesta pasta."""
    sufixo = "-windows.zip" if os.name == "nt" else "-macos-linux.tar.gz"
    asset = next((a for a in release.get("assets") or [] if a.get("name", "").endswith(sufixo)), None)
    if not asset:
        return "erro"
    try:
        dados = _baixar(asset["browser_download_url"], tempo=60)
        with tempfile.TemporaryDirectory() as tmp:
            if sufixo.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(dados)) as z:
                    z.extractall(tmp)
            else:
                with tarfile.open(fileobj=io.BytesIO(dados), mode="r:gz") as t:
                    t.extractall(tmp, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(tmp)
            raiz = os.path.join(tmp, "audiobook-generator-terminal")
            if not os.path.isfile(os.path.join(raiz, "audiobook_cli.py")):
                return "erro"
            for pasta, _, arquivos in os.walk(raiz):
                destino = os.path.join(BASE, os.path.relpath(pasta, raiz))
                os.makedirs(destino, exist_ok=True)
                for nome in arquivos:
                    shutil.copy2(os.path.join(pasta, nome), os.path.join(destino, nome))
        return "ok"
    except Exception:
        return "erro"
