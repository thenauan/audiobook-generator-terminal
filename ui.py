"""
Visual do Audiobook Generator no terminal: cores, ícones, perguntas e barras de progresso.

Tudo o que aparece na tela passa por aqui, para manter um padrão só:

  Cores (papéis)                      Ícones
  ─────────────────────────────       ───────────────────────────────
  destaque  ciano      etapas, teclas  ◆  etapa          ✔  concluído
  texto     branco     valores         ›  pergunta       ✖  erro
  suave     cinza      dicas           •  item de lista  ▲  aviso
  sucesso   verde                      ●  informação     ♪  amostra de voz
  aviso     amarelo                    📚 livros   🎙 voz   🌐 idioma
  erro      vermelho                   📝 texto    💾 pasta 🎧 formato

Sem dependências: usa os códigos ANSI que o Terminal do Windows, o PowerShell, o macOS e o Linux entendem.
"""
import os
import shutil
import sys
import threading
import unicodedata

from i18n import palavras, t

# ---------------------------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------------------------
if os.name == "nt":
    os.system("")  # liga as cores ANSI no console do Windows
try:  # acentos e símbolos mesmo em terminais com locale antigo (LANG=C no Linux, página de código no Windows)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CORES = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"
# Emojis só onde o terminal os desenha bem (Terminal do Windows, macOS, Linux gráfico); no console antigo do
# Windows e no console de texto do Linux (TERM=linux), símbolos simples
if os.name == "nt":
    EMOJI = bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))
else:
    EMOJI = os.environ.get("TERM") != "linux"


def _c(codigo):
    return (lambda t: f"\033[{codigo}m{t}\033[0m") if CORES else (lambda t: str(t))


destaque = _c("96")       # ciano claro
destaque_forte = _c("1;96")
texto = _c("97")          # branco
suave = _c("90")          # cinza
sucesso = _c("92")        # verde
aviso = _c("93")          # amarelo
erro = _c("91")           # vermelho
negrito = _c("1")

ICONE = {
    "etapa": "◆", "pergunta": "›", "item": "•", "info": "●", "ok": "✔", "erro": "✖", "aviso": "▲", "amostra": "♪",
    "livros": "📚" if EMOJI else "■", "voz": "🎙" if EMOJI else "♪", "idioma": "🌐" if EMOJI else "◎",
    "texto": "📝" if EMOJI else "≡", "pasta": "💾" if EMOJI else "▣", "formato": "🎧" if EMOJI else "♫",
    "traduzir": "🔁" if EMOJI else "⇄", "converter": "🚀" if EMOJI else "►",
}


def largura():
    # Uma coluna a menos que o terminal: linha que encosta na borda quebra e desalinha o painel de progresso
    return max(40, min(110, shutil.get_terminal_size((100, 30)).columns - 1))


def visivel(t):
    """Largura do texto na tela (sem os códigos de cor; emojis e ideogramas ocupam 2 colunas)."""
    import re
    limpo = re.sub(r"\033\[[0-9;]*m", "", str(t))
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 0 if unicodedata.combining(c) else 1 for c in limpo)


def cortar(t, n):
    t = str(t)
    if visivel(t) <= n:
        return t
    saida = ""
    for c in t:
        if visivel(saida + c + "…") > n:
            break
        saida += c
    return saida + "…"


# ---------------------------------------------------------------------------------------------
# Blocos de tela
# ---------------------------------------------------------------------------------------------
def banner(nome, versao, slogan):
    w = largura()
    print()
    print(destaque("  " + "─" * (w - 4)))
    print("  " + destaque_forte(f"{ICONE['livros']}  {nome}") + suave(f"  v{versao}"))
    print("  " + suave(slogan))
    print(destaque("  " + "─" * (w - 4)))


def etapa(numero, total, titulo, icone=None):
    print()
    marcador = f"{ICONE[icone]}  " if icone else ""
    print("  " + destaque(f"{ICONE['etapa']} " + t("step", n=numero, total=total)) + "  " + negrito(f"{marcador}{titulo}"))
    print(suave("  " + "─" * (largura() - 4)))


def info(msg):
    print("  " + destaque(ICONE["info"]) + " " + str(msg))


def ok(msg):
    print("  " + sucesso(ICONE["ok"]) + " " + str(msg))


def alerta(msg):
    print("  " + aviso(ICONE["aviso"]) + " " + aviso(str(msg)))


def falha(msg):
    print("  " + erro(ICONE["erro"]) + " " + erro(str(msg)))


def dica(msg):
    print("  " + suave(str(msg)))


def cartao(titulo, linhas):
    """Quadro com o nome do livro e os dados em duas colunas (rótulo, valor)."""
    w = min(largura() - 4, 96)
    print("  " + destaque("╭" + "─" * (w - 2) + "╮"))
    cab = " " + negrito(cortar(titulo, w - 4))
    print("  " + destaque("│") + cab + " " * max(0, w - 2 - visivel(cab)) + destaque("│"))
    print("  " + destaque("├" + "─" * (w - 2) + "┤"))
    rot = max(visivel(r) for r, _ in linhas) if linhas else 0
    for r, v in linhas:
        corpo = " " + suave(r.ljust(rot)) + "  " + texto(cortar(v if v else "—", w - rot - 6))
        print("  " + destaque("│") + corpo + " " * max(0, w - 2 - visivel(corpo)) + destaque("│"))
    print("  " + destaque("╰" + "─" * (w - 2) + "╯"))


# ---------------------------------------------------------------------------------------------
# Perguntas
# ---------------------------------------------------------------------------------------------
def perguntar(rotulo, padrao=""):
    """Texto livre; Enter mantém o padrão."""
    extra = suave(f" [{padrao}]") if padrao else ""
    try:
        r = input("  " + destaque(ICONE["pergunta"]) + " " + rotulo + extra + " ").strip()
    except EOFError:
        r = ""
    return r or padrao


def sim_nao(rotulo, padrao=True):
    op = t("yn_default_yes") if padrao else t("yn_default_no")
    # Respostas no idioma da interface e, em qualquer idioma, y/yes e n/no
    sim = set(palavras("yes_words")) | {"y", "yes"}
    nao = set(palavras("no_words")) | {"n", "no"}
    while True:
        try:
            r = input("  " + destaque(ICONE["pergunta"]) + " " + rotulo + " " + suave(f"({op})") + " ").strip().lower()
        except EOFError:
            r = ""
        if not r:
            return padrao
        if r in sim:
            return True
        if r in nao:
            return False
        alerta(t("answer_yes_no"))


def escolher(rotulo, opcoes, padrao=0, extras=None, colunas=1):
    """Lista numerada. `opcoes`: [(rótulo, detalhe)]. `extras`: {tecla: descrição} para ações como amostras.
    Devolve o índice escolhido ou (tecla, argumento) quando é uma ação extra."""
    w = largura()
    if colunas > 1:
        larg = (w - 6) // colunas
        for i in range(0, len(opcoes), colunas):
            linha = ""
            for j, (r, d) in enumerate(opcoes[i:i + colunas]):
                k = i + j
                marca = destaque_forte("›") if k == padrao else " "
                cel = f"{marca}{destaque(str(k + 1).rjust(3))} {cortar(r, larg - 7)}"
                linha += cel + " " * max(1, larg - visivel(cel))
            print("  " + linha.rstrip())
    else:
        for k, (r, d) in enumerate(opcoes):
            marca = destaque_forte("›") if k == padrao else " "
            print(f"  {marca}{destaque(str(k + 1).rjust(3))}  {r}" + (suave(f"  {d}") if d else ""))
    if extras:
        print("  " + suave("   " + "   ".join(f"{destaque(t)} {d}" for t, d in extras.items())))
    while True:
        r = perguntar(rotulo, str(padrao + 1))
        partes = r.split(maxsplit=1)
        if extras and partes and partes[0].lower() in extras:
            return partes[0].lower(), (partes[1] if len(partes) > 1 else "")
        if r.isdigit() and 1 <= int(r) <= len(opcoes):
            return int(r) - 1
        alerta(t("enter_number", max=len(opcoes)))


# ---------------------------------------------------------------------------------------------
# Progresso das conversões (uma linha por livro, redesenhada no lugar)
# ---------------------------------------------------------------------------------------------
class PainelProgresso:
    def __init__(self, livros):
        self.livros = livros  # lista de dicts com id e titulo
        self.estado = {l["id"]: {"pct": 0.0, "etapa": t("queued"), "partes": "", "vel": "", "fim": None,
                                 "amostras": [], "restante": None, "ultimo_passo": None} for l in livros}
        self.desenhado = 0
        self.pausado = False
        self.trava = threading.Lock()  # o painel é desenhado pela thread dos eventos e pela principal

    def pausar(self):
        """Para de redesenhar (enquanto uma pergunta ocupa a tela); o progresso continua sendo anotado."""
        with self.trava:
            self.pausado = True

    def retomar(self):
        """Volta a desenhar. Depois de uma pausa, o painel recomeça abaixo do que foi escrito na tela."""
        with self.trava:
            if self.pausado:
                self.desenhado = 0
            self.pausado = False
        self.desenhar()

    def atualizar(self, fid, **dados):
        if fid in self.estado:
            e = self.estado[fid]
            e.update({k: v for k, v in dados.items() if v is not None})
            import time as _t
            agora = _t.time()
            if not e["amostras"] or e["amostras"][-1][1] != e["pct"]:
                e["amostras"].append((agora, e["pct"]))
            while len(e["amostras"]) > 2 and agora - e["amostras"][1][0] > 120:
                e["amostras"].pop(1)  # janela de ~2 min, mantendo o ponto inicial

    def _restante(self, e):
        """Tempo restante pelo ritmo real: ritmo do último minuto (60%) + ritmo médio (40%), suavizado,
        descendo de segundo em segundo entre as atualizações."""
        import time as _t
        agora = _t.time()
        a = e["amostras"]
        calculado = None
        if len(a) >= 2 and a[-1][1] - a[0][1] >= 0.5 and agora - a[0][0] >= 8:
            ref = next((x for x in reversed(a) if agora - x[0] >= 60), a[0])
            recente = (e["pct"] - ref[1]) / max(1, agora - ref[0])
            medio = (e["pct"] - a[0][1]) / max(1, agora - a[0][0])
            ritmo = recente * 0.6 + medio * 0.4 if recente > 0 else medio
            calculado = (100 - e["pct"]) / ritmo if ritmo > 0 else None
        anterior, passo = e["restante"], e["ultimo_passo"]
        decorrido = agora - passo if passo else 0
        if calculado is not None:
            e["restante"] = calculado if anterior is None else max(0, anterior - decorrido) * 0.8 + calculado * 0.2
        elif anterior is not None:
            e["restante"] = max(0, anterior - decorrido)
        e["ultimo_passo"] = agora
        if e["restante"] is None:
            return t("calculating")
        s = max(1, int(round(e["restante"])))
        return f"{s // 3600}h {(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"

    def _linha(self, livro):
        e = self.estado[livro["id"]]
        w = largura()
        larg_titulo = min(28, max(14, w // 4))
        titulo = cortar(livro["titulo"], larg_titulo)
        titulo += " " * max(0, larg_titulo - visivel(titulo))
        if e["fim"] == "ok":
            return "  " + sucesso(ICONE["ok"]) + " " + texto(titulo) + "  " + sucesso(t("done"))
        if e["fim"] == "erro":
            return "  " + erro(ICONE["erro"]) + " " + texto(titulo) + "  " + erro(cortar(e["etapa"], w - larg_titulo - 10))
        n = max(8, min(24, w - larg_titulo - 30))  # barra menor em terminais estreitos
        cheio = int(round(e["pct"] / 100 * n))
        barra = destaque("━" * cheio) + suave("━" * (n - cheio))
        resto = f"{e['pct']:5.1f}%  " + e["etapa"] + f" · {self._restante(e)}"
        if e["partes"]:
            resto += f" · {e['partes']}"
        if e["vel"]:
            resto += f" · {e['vel']}"
        return "  " + destaque(ICONE["info"]) + " " + texto(titulo) + "  " + barra + " " + suave(cortar(resto, w - larg_titulo - n - 10))

    def desenhar(self):
        with self.trava:
            if self.pausado:
                return
            if CORES and self.desenhado:
                sys.stdout.write(f"\r\033[{self.desenhado}A")  # volta ao início do painel
            for livro in self.livros:
                sys.stdout.write("\033[2K" + self._linha(livro) + "\n" if CORES else self._linha(livro) + "\n")
            sys.stdout.flush()
            self.desenhado = len(self.livros)
