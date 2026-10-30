"""
Capa genérica para audiobooks de e-books sem capa: a mesma capa que o app mostra (src/lib/covers.ts,
getGenericBookCover) — gradiente na diagonal, o ícone de livro grande ao fundo, título no topo e autor na base —
desenhada com o Pillow para ir dentro do arquivo de áudio.

A cor é a mesma do app: a escolhida pelo usuário ou a automática (mesmo cálculo a partir do título e do autor).
"""
import base64
import io
import math
import os

# As 24 cores automáticas do app (AUTO_COVER_COLORS, na mesma ordem)
CORES_AUTO = ["#F97316", "#F59E0B", "#FACC15", "#84CC16", "#10B981", "#0D9488", "#06B6D4", "#38BDF8",
              "#2563EB", "#4F46E5", "#7C3AED", "#9333EA", "#EC4899", "#F43F5E", "#DC2626", "#991B1B",
              "#1E3A8A", "#14532D", "#1E293B", "#475569", "#FDE68A", "#A7F3D0", "#DDD6FE", "#FED7AA"]

ESCALA = 2  # desenha em 1000x1600 (o dobro do SVG do app) para o texto ficar nítido


# ---------------------------------------------------------------------------------------------
# Cores (mesmas contas do app)
# ---------------------------------------------------------------------------------------------
def _cor_automatica(titulo, autor):
    """hash = charCode + ((hash << 5) - hash), igual ao JS do app: só o deslocamento (<<) trunca para 32 bits;
    a soma e a subtração não. Percorre as unidades UTF-16 (como charCodeAt)."""
    def i32(x):
        x &= 0xFFFFFFFF
        return x - 0x100000000 if x >= 0x80000000 else x

    texto = (titulo or "") + (autor or "")
    h = 0
    dados = texto.encode("utf-16-le")
    for i in range(0, len(dados), 2):
        unidade = dados[i] | (dados[i + 1] << 8)
        h = unidade + (i32(i32(h) << 5) - h)
    return CORES_AUTO[int(abs(h) % len(CORES_AUTO))]


def _hex_para_hsl(hexcor):
    c = hexcor.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    h = s = 0.0
    if mx != mn:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            h = (g - b) / d + (6 if g < b else 0)
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h = round(h * 60)
    return h, round(s * 100), round(l * 100)


def _hsl_para_rgb(h, s, l):
    s = max(0, min(100, s)) / 100
    l = max(0, min(100, l)) / 100
    k = lambda n: (n + h / 30) % 12
    a = s * min(l, 1 - l)
    f = lambda n: l - a * max(-1, min(k(n) - 3, min(9 - k(n), 1)))
    return tuple(int(round(f(n) * 255)) for n in (0, 8, 4))


def _hex_para_rgb(hexcor):
    c = hexcor.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _clara(hexcor):
    r, g, b = _hex_para_rgb(hexcor)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.52


def _tons(hexcor):
    """getCoverTones: início, meio e fim do gradiente, cor do texto e do ícone."""
    h, s, l = _hex_para_hsl(hexcor)
    clara = _clara(hexcor)
    if l >= 72:
        ini = _hsl_para_rgb((h - 8) % 360, min(100, s + 6), min(97, l + 10))
        fim = _hsl_para_rgb((h + 10) % 360, min(100, s + 16), max(48, l - 22))
        txt = _hsl_para_rgb(h, min(100, max(45, s + 15)), 18)
        ico = _hsl_para_rgb(h, min(100, max(40, s + 10)), 44)
    elif clara:
        ini = _hsl_para_rgb((h - 8) % 360, min(100, s + 6), min(92, l + 18))
        fim = _hsl_para_rgb((h + 10) % 360, min(100, s + 8), max(44, l - 18))
        txt = _hsl_para_rgb(h, min(100, max(50, s + 10)), 14)
        ico = _hsl_para_rgb(h, min(100, max(45, s)), 33)
    elif l <= 32:
        ini = _hsl_para_rgb((h - 8) % 360, min(100, s + 10), min(62, l + 22))
        fim = _hsl_para_rgb((h + 8) % 360, min(100, s + 6), max(6, l - 12))
        txt = _hsl_para_rgb(h, min(100, max(38, s + 15)), 86)
        ico = _hsl_para_rgb(h, min(100, max(35, s + 10)), 56)
    else:
        ini = _hsl_para_rgb((h - 8) % 360, min(100, s + 6), min(68, l + 16))
        fim = _hsl_para_rgb((h + 10) % 360, min(100, s + 8), max(14, l - 22))
        txt = _hsl_para_rgb(h, min(100, max(38, s + 12)), 88)
        ico = _hsl_para_rgb(h, min(100, max(35, s)), 66)
    return clara, ini, _hex_para_rgb(hexcor), fim, txt, ico


# ---------------------------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------------------------
def _quebrar_titulo(titulo, por_linha=18, max_linhas=3):
    """wrapTitleForCover: até 3 linhas de ~18 letras; o que sobrar vira reticências."""
    palavras = (titulo or "Audiobook").strip().split()
    linhas, atual = [], ""
    for p in palavras:
        if not atual:
            atual = p
        elif len(atual + " " + p) <= por_linha:
            atual += " " + p
        else:
            linhas.append(atual)
            atual = p
            if len(linhas) == max_linhas - 1:
                break
    if atual:
        linhas.append(atual)
    if len(linhas) == max_linhas and len(palavras) > sum(len(l.split()) for l in linhas):
        linhas[-1] = linhas[-1].rstrip(".") + "..."
    return linhas


def _fonte(tamanho):
    from PIL import ImageFont
    candidatas = [
        "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for c in candidatas:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, tamanho)
            except OSError:
                continue
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", tamanho)
    except OSError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------------------------
# Ícone de livro (o "book-open" do app, ampliado ao fundo)
# ---------------------------------------------------------------------------------------------
def _arco(cx, cy, r, de, ate, passos=12):
    return [(cx + r * math.cos(math.radians(de + (ate - de) * i / passos)),
             cy + r * math.sin(math.radians(de + (ate - de) * i / passos))) for i in range(passos + 1)]


def _paginas():
    """As duas páginas do ícone, no sistema de 24x24 do ícone."""
    esquerda = [(2, 3), (8, 3)] + _arco(8, 7, 4, -90, 0) + [(12, 21)] + _arco(9, 21, 3, 0, -90) + [(2, 18)]
    direita = [(24 - x, y) for x, y in esquerda]
    return esquerda, direita


def gerar_capa_generica(titulo, autor, cor=None):
    """Capa genérica em JPEG (data URL), pronta para ir dentro do audiobook. None se o Pillow não estiver instalado."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    cor = cor if (isinstance(cor, str) and len(cor) == 7 and cor.startswith("#")) else _cor_automatica(titulo, autor)
    clara, ini, meio, fim, cor_texto, cor_icone = _tons(cor)
    W, H = 500 * ESCALA, 800 * ESCALA

    # Fundo: gradiente na diagonal (como o objectBoundingBox do SVG)
    # (o gradiente é linear: desenhado pequeno e ampliado fica igual e sai na hora)
    pw, ph = 100, 160
    img = Image.new("RGB", (pw, ph))
    px = img.load()
    for y in range(ph):
        for x in range(pw):
            t = (x / (pw - 1) + y / (ph - 1)) / 2
            a, b, u = (ini, meio, t * 2) if t < 0.5 else (meio, fim, (t - 0.5) * 2)
            px[x, y] = tuple(int(a[i] + (b[i] - a[i]) * u) for i in range(3))
    img = img.resize((W, H), Image.BILINEAR)

    # Ícone grande e transparente atrás do texto: translate(-20, 16) scale(32)
    camada = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(camada)
    conv = lambda p: ((-20 + 32 * p[0]) * ESCALA, (16 + 32 * p[1]) * ESCALA)
    opac = 0.18 if clara else 0.22
    for pagina in _paginas():
        pts = [conv(p) for p in pagina]
        d.polygon(pts, fill=cor_icone + (int(255 * 0.32 * opac),))
        d.line(pts + [pts[0]], fill=cor_icone + (int(255 * opac),), width=int(1.5 * 32 * ESCALA), joint="curve")
    for x1, y1, x2, y2 in [(5, 7, 9, 7), (5, 11, 9, 11), (15, 7, 19, 7), (15, 11, 19, 11)]:
        d.line([conv((x1, y1)), conv((x2, y2))], fill=cor_icone + (int(255 * opac),), width=int(1.2 * 32 * ESCALA))
    img = Image.alpha_composite(img.convert("RGBA"), camada)

    # Título no topo e autor na base, centralizados
    d = ImageDraw.Draw(img)
    linhas = _quebrar_titulo(titulo)
    tam = 32 if len(linhas) >= 3 else 36
    fonte_titulo = _fonte(tam * ESCALA)
    inicio = 100 + round(tam * 0.72)
    for i, linha in enumerate(linhas):
        d.text((250 * ESCALA, (inicio + i * (tam + 10)) * ESCALA), linha, font=fonte_titulo, fill=cor_texto, anchor="ms")
    nome = (autor or "").strip() or "Autor Desconhecido"
    if len(nome) > 28:
        nome = nome[:26] + "..."
    d.text((250 * ESCALA, 700 * ESCALA), nome, font=_fonte(28 * ESCALA), fill=cor_texto, anchor="ms")

    saida = io.BytesIO()
    img.convert("RGB").save(saida, "JPEG", quality=92, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(saida.getvalue()).decode("ascii")
