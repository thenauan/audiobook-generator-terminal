"""
Prepara para a voz o que ela leria errado: a voz lê o texto ao pé da letra, então "Brasil x Noruega" virava
"Brasil multiplicação Noruega". Vale para qualquer assunto (esporte, leis, medicina, receitas, ciência, religião,
finanças, tecnologia, livros acadêmicos) e para os erros de cada tipo: símbolos, unidades, ordinais, frações, datas,
números romanos, sinais de marcação, caracteres invisíveis de e-books, parágrafos em maiúsculas e emojis.
As tabelas por assunto ficam logo abaixo: ensinar um caso novo é acrescentar uma linha.

Só muda o que vai para a narração. As regras de texto valem para português, inglês e espanhol; notas, marcação e
emojis valem para todos os idiomas (emojis_fala.json traz os nomes em 13 idiomas; nos outros, o emoji é retirado).
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional, Pattern, Tuple


# ----- Proteção: uma regra com defeito deixa só aquele trecho sem ajuste, sem derrubar a conversão -----
FALHAS = []  # (regra, erro, trecho) das falhas desta execução, para testes e diagnóstico
_AVISADAS = set()


def _registrar(regra: str, e: Exception, trecho: str = "") -> None:
    if len(FALHAS) < 200:
        FALHAS.append((regra, f"{type(e).__name__}: {e}", trecho[:80]))
    chave = (regra, type(e).__name__)
    if chave not in _AVISADAS:
        _AVISADAS.add(chave)
        import sys
        print(f"Aviso: ajuste de fala ignorado ({regra}: {type(e).__name__}: {e}) em {trecho[:60]!r}", file=sys.stderr)


def _protegido(troca):
    """Troca de re.sub que, se falhar num trecho, devolve o trecho como estava."""
    if not callable(troca):
        return troca
    def seguro(m: "re.Match[str]") -> str:
        try:
            return troca(m)
        except Exception as e:
            _registrar(getattr(troca, "__name__", "regra"), e, m.group(0))
            return m.group(0)
    return seguro


def _sub(padrao, troca, texto, count=0, flags=0):
    """re.sub com a troca protegida (as regras deste arquivo usam sempre esta)."""
    try:
        return re.sub(padrao, _protegido(troca), texto, count=count, flags=flags)
    except Exception as e:  # troca de texto inválida (grupo inexistente, escape errado...)
        _registrar(str(getattr(padrao, "pattern", padrao))[:60], e, texto[:80])
        return texto


def _etapa(funcao, t: str, *args) -> str:
    """Roda uma etapa inteira; se ela falhar, o texto segue para as próximas etapas como estava."""
    try:
        return funcao(t, *args)
    except Exception as e:
        _registrar(funcao.__name__, e, t)
        return t

PALAVRAS = {
    "pt": {"contra": "contra", "placar": "a", "vezes": "vezes", "por": "por", "igual": "igual a", "mais": "mais", "menos": "menos",
           "ate": "a", "e": "e", "ou": "ou", "horas": "horas", "minutos": "minutos", "graus": "graus", "celsius": "graus Celsius",
           "km_h": "quilômetros por hora", "m_s": "metros por segundo", "m2": "metros quadrados", "m3": "metros cúbicos",
           "km2": "quilômetros quadrados", "arroba": "arroba", "hashtag": "hashtag", "ponto": "ponto", "paragrafo": "parágrafo",
           "cerca": "cerca de", "mais_menos": "mais ou menos", "com": "com", "sem": "sem", "para": "para", "dolares": "dólares",
           "seculo": r"s[ée]c(?:ulo|\.)?", "antes_cristo": "antes de Cristo", "depois_cristo": "depois de Cristo"},
    "en": {"contra": "versus", "placar": "to", "vezes": "times", "por": "by", "igual": "equals", "mais": "plus", "menos": "minus",
           "ate": "to", "e": "and", "ou": "or", "horas": "hours", "minutos": "minutes", "graus": "degrees", "celsius": "degrees Celsius",
           "km_h": "kilometers per hour", "m_s": "meters per second", "m2": "square meters", "m3": "cubic meters",
           "km2": "square kilometers", "arroba": "at", "hashtag": "hashtag", "ponto": "dot", "paragrafo": "section",
           "cerca": "about", "mais_menos": "plus or minus", "com": "with", "sem": "without", "para": "for", "dolares": "dollars",
           "seculo": r"century", "antes_cristo": "BC", "depois_cristo": "AD"},
    "es": {"contra": "contra", "placar": "a", "vezes": "por", "por": "por", "igual": "igual a", "mais": "más", "menos": "menos",
           "ate": "a", "e": "y", "ou": "o", "horas": "horas", "minutos": "minutos", "graus": "grados", "celsius": "grados Celsius",
           "km_h": "kilómetros por hora", "m_s": "metros por segundo", "m2": "metros cuadrados", "m3": "metros cúbicos",
           "km2": "kilómetros cuadrados", "arroba": "arroba", "hashtag": "hashtag", "ponto": "punto", "paragrafo": "párrafo",
           "cerca": "cerca de", "mais_menos": "más o menos", "com": "con", "sem": "sin", "para": "para", "dolares": "dólares",
           "seculo": r"siglo", "antes_cristo": "antes de Cristo", "depois_cristo": "después de Cristo"},
}
# Reis e papas: até o décimo o número é ordinal; depois, cardinal (Luís XIV = Luís quatorze)
ORDINAIS = {
    "pt": ["", "primeiro", "segundo", "terceiro", "quarto", "quinto", "sexto", "sétimo", "oitavo", "nono", "décimo"],
    "es": ["", "primero", "segundo", "tercero", "cuarto", "quinto", "sexto", "séptimo", "octavo", "noveno", "décimo"],
    "en": ["", "the first", "the second", "the third", "the fourth", "the fifth", "the sixth", "the seventh", "the eighth",
           "the ninth", "the tenth"],
}
TITULOS_NUMERADOS = r"[Ii]nciso|[Ii]nc\.|Cap[íi]tulo|Chapter|Parte|Part|Volume|Volumen|Livro|Libro|Book|Tomo|Ato|Acto|Act|Artigo|Art[íi]culo|Article"
UNIDADES = r"(?:mm|cm|m|km|pol|in|ft|px|polegadas|metros|cent[íi]metros|mil[íi]metros|inches|feet|pixels)\b"
LETRA_MAIUSCULA = "A-ZÀ-ÖØ-Þ"

_VALORES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


_ROMANO_VALIDO = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")


def _romano(s: str) -> int:
    s = s.upper()
    total = 0
    for a, b in zip(s, s[1:] + " "):
        v = _VALORES.get(a, 0)
        total += -v if _VALORES.get(b, 0) > v else v
    return total


def _seculo(m: "re.Match[str]") -> str:
    """"século XIX" e "século xix" viram "século 19"; o que não é número romano válido ("século mil") fica como está."""
    romano = m.group(2)
    if not _ROMANO_VALIDO.fullmatch(romano.upper()) or not 1 <= _romano(romano) <= 30:
        return m.group(0)
    return f"{m.group(1)} {_romano(romano)}"


def _vezes_placar_ou_medida(p: Dict[str, str], lang: str):
    """"3 x 1" é placar; "10 x 20 cm" e "1920x1080" são medidas; "2 x 3 = 6" é conta."""
    def trocar(m: "re.Match[str]") -> str:
        a, sinal, b, depois = m.group(1), m.group(2), m.group(3), m.string[m.end():m.end() + 14]
        if re.match(r"\s*=", depois):
            return f"{a} {p['vezes']} {b}"
        if re.match(r"\s*(?:[xX×]\s*\d|" + UNIDADES + ")", depois, re.IGNORECASE) or len(a) > 2 or len(b) > 2 or (a, b) == ("4", "4"):
            return f"{a} {p['por']} {b}"
        antes = m.string[max(0, m.start() - 24):m.start()]
        if sinal == "×" and not re.search(rf"[{LETRA_MAIUSCULA}]\w*\s*$", antes):
            return f"{a} {p['vezes']} {b}"  # o × sem nome de time antes é conta
        return f"{a} {p['placar']} {b}"
    return trocar


# ----- Tabelas por assunto: para ensinar um caso novo, basta acrescentar uma linha -----
# Unidades depois de número: símbolo -> (pt singular, pt plural, en singular, en plural, es singular, es plural)
UNIDADES_FALA = {
    "mg": ("miligrama", "miligramas", "milligram", "milligrams", "miligramo", "miligramos"),
    "g": ("grama", "gramas", "gram", "grams", "gramo", "gramos"),
    "kg": ("quilo", "quilos", "kilogram", "kilograms", "kilo", "kilos"),
    "mcg": ("micrograma", "microgramas", "microgram", "micrograms", "microgramo", "microgramos"),
    "µg": ("micrograma", "microgramas", "microgram", "micrograms", "microgramo", "microgramos"),
    "ml": ("mililitro", "mililitros", "milliliter", "milliliters", "mililitro", "mililitros"),
    "mL": ("mililitro", "mililitros", "milliliter", "milliliters", "mililitro", "mililitros"),
    "l": ("litro", "litros", "liter", "liters", "litro", "litros"),
    "L": ("litro", "litros", "liter", "liters", "litro", "litros"),
    "mm": ("milímetro", "milímetros", "millimeter", "millimeters", "milímetro", "milímetros"),
    "cm": ("centímetro", "centímetros", "centimeter", "centimeters", "centímetro", "centímetros"),
    "m": ("metro", "metros", "meter", "meters", "metro", "metros"),
    "km": ("quilômetro", "quilômetros", "kilometer", "kilometers", "kilómetro", "kilómetros"),
    "ha": ("hectare", "hectares", "hectare", "hectares", "hectárea", "hectáreas"),
    "kcal": ("quilocaloria", "quilocalorias", "kilocalorie", "kilocalories", "kilocaloría", "kilocalorías"),
    "mmHg": ("milímetro de mercúrio", "milímetros de mercúrio", "millimeter of mercury", "millimeters of mercury", "milímetro de mercurio", "milímetros de mercurio"),
    "bpm": ("batimento por minuto", "batimentos por minuto", "beat per minute", "beats per minute", "latido por minuto", "latidos por minuto"),
    "kW": ("quilowatt", "quilowatts", "kilowatt", "kilowatts", "kilovatio", "kilovatios"),
    "kWh": ("quilowatt-hora", "quilowatts-hora", "kilowatt-hour", "kilowatt-hours", "kilovatio-hora", "kilovatios-hora"),
    "MW": ("megawatt", "megawatts", "megawatt", "megawatts", "megavatio", "megavatios"),
    "Hz": ("hertz", "hertz", "hertz", "hertz", "hercio", "hercios"),
    "kHz": ("quilohertz", "quilohertz", "kilohertz", "kilohertz", "kilohercio", "kilohercios"),
    "MHz": ("mega-hertz", "mega-hertz", "megahertz", "megahertz", "megahercio", "megahercios"),
    "GHz": ("giga-hertz", "giga-hertz", "gigahertz", "gigahertz", "gigahercio", "gigahercios"),
    "KB": ("quilobyte", "quilobytes", "kilobyte", "kilobytes", "kilobyte", "kilobytes"),
    "MB": ("megabyte", "megabytes", "megabyte", "megabytes", "megabyte", "megabytes"),
    "GB": ("gigabyte", "gigabytes", "gigabyte", "gigabytes", "gigabyte", "gigabytes"),
    "TB": ("terabyte", "terabytes", "terabyte", "terabytes", "terabyte", "terabytes"),
    "Mbps": ("megabit por segundo", "megabits por segundo", "megabit per second", "megabits per second", "megabit por segundo", "megabits por segundo"),
    "lb": ("libra", "libras", "pound", "pounds", "libra", "libras"),
    "lbs": ("libra", "libras", "pound", "pounds", "libra", "libras"),
    "oz": ("onça", "onças", "ounce", "ounces", "onza", "onzas"),
    "ft": ("pé", "pés", "foot", "feet", "pie", "pies"),
    "mph": ("milha por hora", "milhas por hora", "mile per hour", "miles per hour", "milla por hora", "millas por hora"),
    "rpm": ("rotação por minuto", "rotações por minuto", "revolution per minute", "revolutions per minute", "revolución por minuto", "revoluciones por minuto"),
}
_COLUNA = {"pt": 0, "en": 2, "es": 4}
# Depois da barra, numa unidade: "5 mg/dia", "10 mg/kg"
POR_UNIDADE = {
    "pt": {"dia": "por dia", "h": "por hora", "hora": "por hora", "min": "por minuto", "mês": "por mês", "ano": "por ano", "semana": "por semana", "kg": "por quilo", "m²": "por metro quadrado"},
    "en": {"day": "per day", "h": "per hour", "hour": "per hour", "min": "per minute", "month": "per month", "year": "per year", "week": "per week", "kg": "per kilogram"},
    "es": {"día": "por día", "h": "por hora", "hora": "por hora", "min": "por minuto", "mes": "por mes", "año": "por año", "semana": "por semana", "kg": "por kilo"},
}
# Abreviações com ponto que dependem do assunto (acadêmico, jurídico, técnico): texto -> fala
ABREVIACOES_TEMA = {
    "pt": {r"\bpp\.(?=\s*\d)": "páginas", r"\bp\.(?=\s*\d)": "página", r"\b[Ff]ig\.(?=\s*\d)": "figura", r"\b[Tt]ab\.(?=\s*\d)": "tabela",
           r"\b[Aa]rts\.(?=\s)": "artigos", r"\b[Aa]rt\.(?=\s)": "artigo", r"\b[Ii]nc\.(?=\s)": "inciso", r"\bal\.(?=\s*[a-z]\b)": "alínea",
           r"\bet al\.": "e outros", r"\bop\. cit\.": "obra citada", r"\b[Ii]bid\.": "ibidem", r"\b[Cc]f\.": "conforme",
           r"\bp\. ?ex\.": "por exemplo", r"\bEx\.:": "Por exemplo:", r"\bex\.:": "por exemplo:", r"\bi\.é\.": "isto é", r"\b[Oo]bs\.:?": "observação:",
           r"\b[Aa]prox\.": "aproximadamente", r"\bmáx\.": "máximo", r"\bmín\.": "mínimo", r"\b[Tt]el\.(?=[\s:])": "telefone", r"\b[Aa]pto\.": "apartamento",
           r"\bV\. ?Exa\.": "Vossa Excelência", r"\bIlmo\.": "Ilustríssimo", r"\bIlma\.": "Ilustríssima", r"\bDr\.ª": "Doutora", r"\bN\. ?T\.": "nota do tradutor",
           r"\bN\. ?E\.": "nota do editor", r"\bP\. ?S\.": "pós-escrito", r"\betc\.": "etcétera", r"\bp\. ?p\.": "pontos percentuais",
           r"\b[Jj]an\.(?=\s*(?:de\s*)?\d)": "janeiro", r"\b[Ff]ev\.(?=\s*(?:de\s*)?\d)": "fevereiro", r"\b[Mm]ar\.(?=\s*(?:de\s*)?\d)": "março",
           r"\b[Aa]br\.(?=\s*(?:de\s*)?\d)": "abril", r"\b[Jj]un\.(?=\s*(?:de\s*)?\d)": "junho", r"\b[Jj]ul\.(?=\s*(?:de\s*)?\d)": "julho",
           r"\b[Aa]go\.(?=\s*(?:de\s*)?\d)": "agosto", r"\b[Ss]et\.(?=\s*(?:de\s*)?\d)": "setembro", r"\b[Oo]ut\.(?=\s*(?:de\s*)?\d)": "outubro",
           r"\b[Nn]ov\.(?=\s*(?:de\s*)?\d)": "novembro", r"\b[Dd]ez\.(?=\s*(?:de\s*)?\d)": "dezembro", r"\bCF/88\b": "Constituição Federal de 1988"},
    "en": {r"\bpp\.(?=\s*\d)": "pages", r"\bp\.(?=\s*\d)": "page", r"\b[Ff]ig\.(?=\s*\d)": "figure", r"\bet al\.": "and others", r"\b[Ii]bid\.": "ibidem",
           r"\b[Cc]f\.": "compare", r"\b[Aa]pprox\.": "approximately", r"\bP\. ?S\.": "postscript", r"\bPh\. ?D\.": "PhD", r"\bInc\.": "Incorporated",
           r"\bCo\.(?=\s|,)": "Company", r"\bvol\.(?=\s*\d)": "volume", r"\bed\.(?=\s*\d)": "edition", r"\ba\.k\.a\.": "also known as"},
    "es": {r"\bpp\.(?=\s*\d)": "páginas", r"\bp\.(?=\s*\d)": "página", r"\b[Ff]ig\.(?=\s*\d)": "figura", r"\bet al\.": "y otros", r"\b[Ii]bid\.": "ibídem",
           r"\b[Cc]f\.": "véase", r"\b[Aa]prox\.": "aproximadamente", r"\bnúm\.(?=\s*\d)": "número", r"\b[Aa]rt\.(?=\s)": "artículo", r"\betc\.": "etcétera",
           r"\bp\. ?ej\.": "por ejemplo"},
}
# Símbolos de matemática e ciência
SIMBOLOS = {
    "pt": {"≥": "maior ou igual a", "≤": "menor ou igual a", "≠": "diferente de", "≈": "aproximadamente", "√": "raiz quadrada de", "∞": "infinito",
           "∑": "somatório", "∆": "delta", "‰": " por mil", "÷": "dividido por"},
    "en": {"≥": "greater than or equal to", "≤": "less than or equal to", "≠": "not equal to", "≈": "approximately", "√": "square root of",
           "∞": "infinity", "∑": "sum", "∆": "delta", "‰": " per thousand", "÷": "divided by"},
    "es": {"≥": "mayor o igual que", "≤": "menor o igual que", "≠": "distinto de", "≈": "aproximadamente", "√": "raíz cuadrada de", "∞": "infinito",
           "∑": "sumatoria", "∆": "delta", "‰": " por mil", "÷": "dividido por"},
}
GREGO = {"α": "alfa", "β": "beta", "γ": "gama", "δ": "delta", "ε": "épsilon", "θ": "teta", "λ": "lambda", "μ": "mi", "π": "pi", "σ": "sigma",
         "φ": "fi", "ω": "ômega", "Δ": "delta", "Σ": "sigma", "Ω": "ômega"}
FRACOES = {
    "pt": {"½": "meio", "⅓": "um terço", "⅔": "dois terços", "¼": "um quarto", "¾": "três quartos", "⅛": "um oitavo"},
    "en": {"½": "one half", "⅓": "one third", "⅔": "two thirds", "¼": "one quarter", "¾": "three quarters", "⅛": "one eighth"},
    "es": {"½": "medio", "⅓": "un tercio", "⅔": "dos tercios", "¼": "un cuarto", "¾": "tres cuartos", "⅛": "un octavo"},
}
FEMININOS = {"pt": r"x[íi]caras?|colher(?:es)?|horas?|d[úu]zias?|latas?|garrafas?|fatias?|cebolas?|laranjas?|p[áa]ginas?|porç[ãa]o",
             "es": r"tazas?|cucharadas?|horas?|docenas?|latas?|botellas?|cebollas?|naranjas?|p[áa]ginas?"}
FRACOES_TEXTO = {"1/2": "½", "1/3": "⅓", "2/3": "⅔", "1/4": "¼", "3/4": "¾", "1/8": "⅛"}
# Termos técnicos e fórmulas que a voz soletraria mal
TERMOS = {
    "pt": {"C++": "C mais mais", "C#": "C sharp", ".NET": "dot net", "H2O": "agá dois ó", "CO2": "cê ó dois", "O2": "ó dois", "24/7": "24 horas por dia, 7 dias por semana"},
    "en": {"C++": "C plus plus", "C#": "C sharp", ".NET": "dot net", "H2O": "H two O", "CO2": "C O two", "24/7": "twenty-four seven"},
    "es": {"C++": "C más más", "C#": "C sharp", ".NET": "dot net", "H2O": "hache dos o", "CO2": "ce o dos", "24/7": "24 horas al día, 7 días a la semana"},
}
# Citações bíblicas: "Jo 3:16"
LIVROS_BIBLIA = {
    "pt": {"Gn": "Gênesis", "Êx": "Êxodo", "Lv": "Levítico", "Nm": "Números", "Dt": "Deuteronômio", "Js": "Josué", "Jz": "Juízes", "Rt": "Rute", "Jó": "Jó",
           "Sl": "Salmos", "Pv": "Provérbios", "Ec": "Eclesiastes", "Is": "Isaías", "Jr": "Jeremias", "Ez": "Ezequiel", "Dn": "Daniel", "Mt": "Mateus",
           "Mc": "Marcos", "Lc": "Lucas", "Jo": "João", "At": "Atos", "Rm": "Romanos", "Gl": "Gálatas", "Ef": "Efésios", "Fp": "Filipenses", "Cl": "Colossenses",
           "Hb": "Hebreus", "Tg": "Tiago", "Ap": "Apocalipse"},
    "en": {"Gen": "Genesis", "Exod": "Exodus", "Lev": "Leviticus", "Deut": "Deuteronomy", "Ps": "Psalm", "Prov": "Proverbs", "Isa": "Isaiah", "Jer": "Jeremiah",
           "Matt": "Matthew", "Rom": "Romans", "Gal": "Galatians", "Eph": "Ephesians", "Heb": "Hebrews", "Rev": "Revelation"},
    "es": {"Gn": "Génesis", "Sal": "Salmos", "Pr": "Proverbios", "Is": "Isaías", "Mt": "Mateo", "Mc": "Marcos", "Lc": "Lucas", "Jn": "Juan", "Hch": "Hechos",
           "Ro": "Romanos", "Ap": "Apocalipsis"},
}
BIBLIA_FALA = {"pt": ("capítulo", "versículo"), "en": ("chapter", "verse"), "es": ("capítulo", "versículo")}
MESES = {
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
}
GRANDEZAS = {"pt": {"mi": "milhões", "bi": "bilhões", "tri": "trilhões"}, "es": {"mi": "millones"}}
MIL = {"pt": "mil", "en": "thousand", "es": "mil"}
PALAVRAS_ORDINAL = r"lugar|colocad[oa]|ano|andar|dia|m[êe]s|tempo|turno|semestre|per[íi]odo|grau|s[ée]rie|batalh[ãa]o|tomo|volume|edi[çc][ãa]o|vez|feira|classe"
DEZENAS_PT = ["", "décimo", "vigésimo", "trigésimo", "quadragésimo", "quinquagésimo", "sexagésimo", "septuagésimo", "octogésimo", "nonagésimo"]
SOBRESCRITOS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
SUBSCRITOS = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
ELEVADO = {"pt": "elevado a", "en": "to the power of", "es": "elevado a"}
QUADRADO = {"pt": ("ao quadrado", "ao cubo"), "en": ("squared", "cubed"), "es": ("al cuadrado", "al cubo")}


def _ordinal(n: int, feminino: bool, lang: str) -> str:
    base = ORDINAIS[lang]
    if lang == "en" or n <= 0:
        return str(n)
    if n <= 10:
        fala = base[n]
    elif lang == "pt" and n < 100:
        fala = DEZENAS_PT[n // 10] + (f" {base[n % 10]}" if n % 10 else "")
    else:
        return str(n)
    return _sub(r"o\b", "a", fala) if feminino else fala


def _ordinais(t: str, lang: str) -> str:
    """1º, 2ª, 3.º e "1° lugar" (o ° depois do número, sem palavra de ordem, continua sendo grau)."""
    if lang == "en":
        return t
    t = _sub(r"(?<![\d.,])(\d{1,2})\.?\s?([ºª])(?!\w)", lambda m: _ordinal(int(m.group(1)), m.group(2) == "ª", lang), t)
    return _sub(rf"(?<![\d.,])(\d{{1,2}})°(?=\s+(?:{PALAVRAS_ORDINAL})\b)", lambda m: _ordinal(int(m.group(1)), False, lang), t, flags=re.IGNORECASE)


def _regras_por_tema(t: str, lang: str) -> str:
    """Regras que valem para qualquer assunto: leis, ciência, medicina, receitas, religião, finanças, tecnologia."""
    p = PALAVRAS[lang]
    # Leis: "Lei nº 8.666/93", "Decreto 3.048/1999"
    numero = {"pt": "número", "en": "number", "es": "número"}[lang]
    de = {"pt": "de", "en": "of", "es": "de"}[lang]
    t = _sub(r"\b(Lei|Decreto|Portaria|Resolu[çc][ãa]o|Emenda|Medida Provis[óo]ria|Ley|Act)\s+(?:n[.º°o]*\s*)?([\d.]+)/(\d{2,4})\b",
               rf"\1 {numero} \2 {de} \3", t)
    # Bíblia: "Jo 3:16", "Sl 23.1"
    livros = LIVROS_BIBLIA[lang]
    cap, vers = BIBLIA_FALA[lang]
    t = _sub(r"\b(" + "|".join(map(re.escape, livros)) + r")\.?\s(\d{1,3})[:.,](\d{1,3})\b",
               lambda m: f"{livros[m.group(1)]} {cap} {m.group(2)} {vers} {m.group(3)}", t)
    # Datas completas: 12/05/1990 (em inglês a ordem é ambígua: fica como está)
    if lang in MESES:
        def data(m: "re.Match[str]") -> str:
            dia, mes, ano = int(m.group(1)), int(m.group(2)), m.group(3)
            if not (1 <= dia <= 31 and 1 <= mes <= 12):
                return m.group(0)
            return f"{dia} de {MESES[lang][mes - 1]} de {ano}"
        t = _sub(r"(?<![\d/.])(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?![\d/])", data, t)
    # Abreviações do assunto e termos técnicos
    for padrao, fala in ABREVIACOES_TEMA[lang].items():
        t = _sub(padrao, fala, t)
    t = _sub(r"\b(p[áa]ginas|pages)\s+(\d+)\s*[-–—]\s*(\d+)", rf"\1 \2 {p['ate']} \3", t)
    for termo, fala in TERMOS[lang].items():
        t = _sub(rf"(?<![\w+#.]){re.escape(termo)}(?![\w+#])", fala, t)
    # Frações: "1/2 xícara", "1 ½"
    t = _sub(r"(?<![\d/.,])([123])/([248 3])(?=\s+[^\W\d_])", lambda m: FRACOES_TEXTO.get(m.group(0), m.group(0)), t)
    for sinal, fala in FRACOES[lang].items():
        t = _sub(rf"(?<=\d)\s*{sinal}", f" {p['e']} {fala}", t)
        t = t.replace(sinal, fala)
    if lang in FEMININOS:  # "meio" concorda com a palavra seguinte: meia xícara
        t = _sub(rf"\b(m)e(?:io|dio)(\s+(?:de\s+)?(?:{FEMININOS[lang]})\b)", lambda m: {"pt": "meia", "es": "media"}[lang] + m.group(2), t)
    # Potências: 10⁶, 10^6, x²; subscritos de fórmulas: CO₂
    t = _sub(r"(?<=\d)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)", lambda m: f" {ELEVADO[lang]} {m.group(1).translate(SOBRESCRITOS)}", t)
    t = _sub(r"(?<=\d)\^(\d+)", rf" {ELEVADO[lang]} \1", t)
    t = _sub(r"(?<![^\W\d_])([a-zA-Z])²(?!\w)", rf"\1 {QUADRADO[lang][0]}", t)
    t = _sub(r"(?<![^\W\d_])([a-zA-Z])³(?!\w)", rf"\1 {QUADRADO[lang][1]}", t)
    t = t.translate(SUBSCRITOS)
    for termo, fala in TERMOS[lang].items():  # CO₂ acabou de virar CO2
        t = _sub(rf"(?<![\w+#.]){re.escape(termo)}(?![\w+#])", fala, t)
    for sinal, fala in SIMBOLOS[lang].items():
        t = t.replace(sinal, f" {fala} ")
    t = _sub(r"(?<=[\w)])\s+<\s+(?=[\w(])", {"pt": " menor que ", "en": " less than ", "es": " menor que "}[lang], t)
    t = _sub(r"(?<=[\w)])\s+>\s+(?=[\w(])", {"pt": " maior que ", "en": " greater than ", "es": " mayor que "}[lang], t)
    t = _sub(r"(?<![^\W\d_])[αβγδεθλμπσφωΔΣΩ](?![^\W\d_])", lambda m: GREGO[m.group(0)], t)
    # Valores: "R$ 5 mi", "10k"
    for curto, fala in GRANDEZAS.get(lang, {}).items():
        t = _sub(rf"((?:R\$|US\$|€)\s?\d[\d.,]*)\s?{curto}\b", rf"\1 {fala}", t)
    t = _sub(r"(?<![\w.,])(\d{1,3})k(?!\w)", rf"\1 {MIL[lang]}", t)
    # Unidades depois de número, com singular e plural, e "mg/dia"
    coluna = _COLUNA[lang]

    def unidade(m: "re.Match[str]") -> str:
        valor, simbolo, por = m.group(1), m.group(2), m.group(3)
        fala = UNIDADES_FALA[simbolo][coluna + (0 if valor == "1" else 1)]
        extra = f" {POR_UNIDADE[lang][por]}" if por else ""
        return f"{valor} {fala}{extra}"
    simbolos = "|".join(sorted(map(re.escape, UNIDADES_FALA), key=len, reverse=True))
    pors = "|".join(sorted(map(re.escape, POR_UNIDADE[lang]), key=len, reverse=True))
    return _sub(rf"(?<![\w.,])(\d[\d.,]*)\s?({simbolos})(?:/({pors}))?(?![\w/²³])", unidade, t)


def _limpar_caracteres(t: str) -> str:
    """Caracteres invisíveis e de composição que vêm de e-books e PDFs e quebram a palavra na voz (todos os idiomas)."""
    t = _sub("[­​‌⁠﻿]", "", t)
    t = t.replace(" ", " ").replace(" ", " ")
    for de, para in (("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬀ", "ff"), ("ﬃ", "ffi"), ("ﬄ", "ffl")):
        t = t.replace(de, para)
    t = _sub(r"\s?\[(?:\.{3}|…)\]|\s?\((?:\.{3}|…)\)", "", t)  # [...] de citação cortada
    return t


def _suavizar_caixa_alta(t: str) -> str:
    """Parágrafo inteiro em maiúsculas: a voz pode soletrar as palavras. Vira texto normal e mantém as siglas
    que o próprio livro usa em maiúsculas nos parágrafos comuns."""
    def so_maiusculas(linha: str) -> bool:
        palavras = re.findall(r"[^\W\d_]{2,}", linha)
        return len(palavras) >= 4 and sum(w.isupper() for w in palavras) / len(palavras) >= 0.9
    linhas = t.split("\n")
    if not any(so_maiusculas(l) for l in linhas):
        return t
    siglas = {w for l in linhas if not so_maiusculas(l) for w in re.findall(r"\b[^\W\d_]{2,6}\b", l) if w.isupper()}

    def normal(linha: str) -> str:
        baixa = _sub(r"[^\W\d_]+", lambda m: m.group(0) if m.group(0) in siglas else m.group(0).lower(), linha)
        return _sub(r"(^\W*|[.!?…]\s+\W*)([^\W\d_])", lambda m: m.group(1) + m.group(2).upper(), baixa)
    return "\n".join(normal(l) if so_maiusculas(l) else l for l in linhas)


def _regras_de_texto(t: str, lang: str) -> str:
    p = PALAVRAS[lang]
    t = _ordinais(t, lang)
    # Partidas e comparações: "Brasil x Noruega", "BRASIL X NORUEGA", "Flamengo vs. Vasco"
    t = _sub(rf"(?<=[^\W\d_]{{2}})\s+[x×]\s+(?=[{LETRA_MAIUSCULA}][^\W\d_])", f" {p['contra']} ", t)
    t = _sub(rf"(?<=[{LETRA_MAIUSCULA}]{{2}})\s+X\s+(?=[{LETRA_MAIUSCULA}]{{2}})", f" {p['contra']} ", t)
    t = _sub(r"(?<=\w)\s+vs\.?\s+(?=\w)", f" {p['contra']} ", t, flags=re.IGNORECASE)

    # Placares, medidas e contas entre números; "2x mais"
    t = _sub(r"(?<![\d.,])(\d+)\s*([xX×])\s*(\d+)(?![\d.,]*\d)", _vezes_placar_ou_medida(p, lang), t)
    t = _sub(r"(?<![\w.,])(\d+)x(?=\s|[.,;!?]|$)", rf"\1 {p['vezes']}", t, flags=re.MULTILINE)

    # Contas e símbolos soltos
    t = _sub(r"(?<=\d)\s*=\s*(?=\d)", f" {p['igual']} ", t)
    t = _sub(r"(?<=[\w)²³])\s+=\s+(?=[\w(])", f" {p['igual']} ", t)
    t = _sub(r"(?<=[\w)²³])\s+\+\s+(?=[\w(])", f" {p['mais']} ", t)
    t = _sub(r"(?<=\d)\s+[-−]\s+(?=\d)", f" {p['menos']} ", t)
    t = t.replace("±", f" {p['mais_menos']} ")
    t = _sub(r"(?<=[^\W\d_])\s*&\s*(?=[^\W\d_])", f" {p['e']} ", t)
    t = _sub(r"\b(e|and|y)/(ou|or|o)\b", lambda m: f"{m.group(1)} {m.group(2)}", t, flags=re.IGNORECASE)
    t = _sub(r"(?<![\w~])~\s*(?=\d)", f"{p['cerca']} ", t)
    t = _sub(r"§§?\s*", f"{p['paragrafo']} ", t)
    t = _sub(r"\s*(?:→|⇒|->|=>)\s*", ", ", t)

    # Períodos: "1939-1945" e "1939–45"
    t = _sub(r"\b(\d{4})\s*[-–—]\s*(\d{2,4})\b", rf"\1 {p['ate']} \2", t)

    # Horários e medidas
    if lang != "en":
        t = _sub(r"\b(\d{1,2})h(\d{2})\b", rf"\1 {p['horas']} {p['e']} \2", t)
        t = _sub(r"\b(\d{1,2})h\b", rf"\1 {p['horas']}", t)
        t = _sub(r"\b(\d{1,3})min\b", rf"\1 {p['minutos']}", t)
        t = _sub(r"(?<=\s)(\d{1,3})['’](?=[\s.,;!?)]|$)", rf"\1 {p['minutos']}", t, flags=re.MULTILINE)  # gol aos 45'
    t = _sub(r"(?<=\d)\s*°\s*C\b", f" {p['celsius']}", t)
    t = _sub(r"(?<=\d)\s*°(?![\w])", f" {p['graus']}", t)
    t = _sub(r"(?<=\d)\s*km/h\b", f" {p['km_h']}", t, flags=re.IGNORECASE)
    t = _sub(r"(?<=\d)\s*m/s\b", f" {p['m_s']}", t)
    t = _sub(r"(?<=\d)\s*km(?:²|2)(?!\w)", f" {p['km2']}", t)
    t = _sub(r"(?<=\d)\s*m(?:²|2)(?!\w)", f" {p['m2']}", t)
    t = _sub(r"(?<=\d)\s*m(?:³|3)(?!\w)", f" {p['m3']}", t)
    def dolares(m: "re.Match[str]") -> str:
        grande = m.group(2) or ""
        de = " de" if lang != "en" and grande and not grande.strip().startswith("mil ") and grande.strip() != "mil" else ""
        return f"{m.group(1)}{grande}{de} {p['dolares']}"
    t = _sub(r"US\$\s?(\d[\d.,]*)(\s(?:milh[õo]es|milh[ãa]o|bilh[õo]es|bilh[ãa]o|millones|mill[óo]n|million|billion|mil)\b)?", dolares, t)

    # Abreviações de bilhete e de datas
    if lang == "pt":
        t = _sub(r"(?<![\w/])c/(?=\s*[^\W\d_])", f"{p['com']} ", t)
        t = _sub(r"(?<![\w/])s/(?=\s*[^\W\d_])", f"{p['sem']} ", t)
        t = _sub(r"(?<![\w/])p/(?=\s*[^\W\d_])", f"{p['para']} ", t)
    if lang in ("pt", "es"):
        t = _sub(r"\ba\.\s?C\.", p["antes_cristo"], t)
        t = _sub(r"\bd\.\s?C\.", p["depois_cristo"], t)
    if lang == "en":
        t = _sub(r"\be\.g\.,?", "for example", t)
        t = _sub(r"\bi\.e\.,?", "that is", t)

    # Números romanos
    t = _sub(rf"\b({p['seculo']})\s+([IVXLCDM]+)\b", _seculo, t, flags=re.IGNORECASE)
    t = _sub(rf"\b({TITULOS_NUMERADOS})\s+([IVXLC]+)\b", lambda m: f"{m.group(1)} {_romano(m.group(2))}", t)
    ordinais = ORDINAIS[lang]

    def regente(m: "re.Match[str]") -> str:
        n = _romano(m.group(2))
        return f"{m.group(1)} {ordinais[n] if n <= 10 else n}"
    # Só com duas letras ou mais: "Vitamina C", "Geração X" e "Plano D" continuam como estão
    t = _sub(rf"\b([{LETRA_MAIUSCULA}][a-zà-ÿ]+)\s+([IVX]{{2,}})\b", regente, t)

    t = _regras_por_tema(t, lang)

    # Redes e endereços
    t = _sub(r"(?<![\w@])@(\w{2,})", rf"{p['arroba']} \1", t)
    t = _sub(r"(?<![\w#])#([^\W\d_]\w+)", rf"{p['hashtag']} \1", t)
    t = _sub(r"\bhttps?://(?:www\.)?([\w-]+(?:\.[\w-]+)+)[^\s)\]]*",
               lambda m: m.group(1).replace(".", f" {p['ponto']} "), t)
    t = _sub(r"\bwww\.([\w-]+(?:\.[\w-]+)+)[^\s)\]]*", lambda m: m.group(1).replace(".", f" {p['ponto']} "), t)
    return t


def _marcacao(t: str) -> str:
    """Sinais que a voz leraria ("asterisco", "colchete um") e não fazem parte da frase."""
    t = _sub(r"(?<=[^\W\d_])[¹²³⁴⁵⁶⁷⁸⁹⁰]+(?=[\s.,;:!?)]|$)", "", t, flags=re.MULTILINE)  # notas: palavra¹ (m² vem antes)
    t = _sub(r"\s?\[\d{1,3}\]", "", t)                       # notas: palavra[12]
    t = _sub(r"^[ \t]*(?:[*\-_=~#•·] ?){3,}[ \t]*$", "", t, flags=re.MULTILINE)  # divisórias: * * *, ---, ===
    t = _sub(r"^[ \t]*[•▪◦‣*][ \t]+", "", t, flags=re.MULTILINE)  # marcadores de lista
    t = _sub(r"\*{1,3}([^*\n]+?)\*{1,3}", r"\1", t)           # **negrito** e *itálico*
    t = _sub(r"(?<!\w)_{1,2}([^_\n]+?)_{1,2}(?!\w)", r"\1", t)  # _itálico_
    t = _sub(r"_{3,}", " ", t)                                # linha de preencher
    return t


# ----- Emojis -----
_EMOJIS: Optional[Dict[str, Dict[str, str]]] = None
_REGEX_EMOJI: Dict[str, Pattern[str]] = {}
_TOM_E_VARIACAO = re.compile("[\U0001F3FB-\U0001F3FF️]")
_TECLA = re.compile("([0-9#*])️?⃣")
# Faixas de emoji e símbolos pictóricos (o que sobrar sem nome é retirado, a voz não sabe ler)
_FAIXAS = "\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿■-◿〰〽㊗㊙‍"
_TEM_EMOJI = re.compile(f"[{_FAIXAS}]")
_SOBRA = re.compile(f"[{_FAIXAS}]+")


def _dados_emojis() -> Dict[str, Dict[str, str]]:
    global _EMOJIS
    if _EMOJIS is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "emojis_fala.json"), encoding="utf-8") as f:
                _EMOJIS = json.load(f)
        except (OSError, ValueError):
            _EMOJIS = {}
    return _EMOJIS


def _regex_emojis(lang: str) -> Tuple[Optional[Pattern[str]], Dict[str, str]]:
    nomes = _dados_emojis().get(lang) or {}
    if not nomes:
        return None, nomes
    if lang not in _REGEX_EMOJI:
        _REGEX_EMOJI[lang] = re.compile("|".join(re.escape(k) for k in sorted(nomes, key=len, reverse=True)))
    return _REGEX_EMOJI[lang], nomes


def ler_emojis(t: str, lang: str) -> str:
    """Troca cada emoji pelo nome no idioma da voz; vários iguais seguidos são lidos uma vez só."""
    t = _TECLA.sub(r"\1", t)  # 1️⃣ vira 1
    if not _TEM_EMOJI.search(t):
        return t
    t = _TOM_E_VARIACAO.sub("", t)
    regex, nomes = _regex_emojis(lang)
    if regex is not None:
        anterior = {"nome": None, "fim": -1}

        def nomear(m: "re.Match[str]") -> str:
            nome = nomes[m.group(0)]
            repetido = nome == anterior["nome"] and not m.string[anterior["fim"]:m.start()].strip()
            anterior["nome"], anterior["fim"] = nome, m.end()
            return "" if repetido else f" {nome} "
        t = regex.sub(_protegido(nomear), t)
    t = _SOBRA.sub(" ", t)
    t = _sub(r"[ \t]+([.,;:!?)])", r"\1", t)
    return t


def preparar_para_fala(texto: str, lang: str) -> str:
    """Ajusta o texto para a voz no idioma dado (código de duas letras)."""
    if not texto:
        return ""
    t = _etapa(_limpar_caracteres, texto)
    t = _etapa(_suavizar_caixa_alta, t)
    if lang in PALAVRAS:
        t = _etapa(_regras_de_texto, t, lang)
    t = _etapa(_marcacao, t)
    return _etapa(ler_emojis, t, lang)
