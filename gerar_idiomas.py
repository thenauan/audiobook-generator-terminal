"""Gera dados/idiomas.json: o nome de cada idioma e região das vozes (dados/vozes.json) em cada idioma
da interface, a partir do CLDR (biblioteca Babel). Só para manutenção — o app não depende do Babel:
    python -m pip install babel
    python gerar_idiomas.py
Os nomes em português que já estão no arquivo são mantidos (foram revisados à mão)."""
import json
import os
import re

from babel import Locale

BASE = os.path.dirname(os.path.abspath(__file__))
INTERFACE = ["en", "es", "pt", "ru", "ar", "fr", "it", "tr", "hi", "zh", "ur"]

with open(os.path.join(BASE, "dados", "vozes.json"), encoding="utf-8") as f:
    vozes = json.load(f)
caminho = os.path.join(BASE, "dados", "idiomas.json")
with open(caminho, encoding="utf-8") as f:
    atual = json.load(f)

codigos = sorted({v["l"].split("-")[0] for v in vozes})
regioes = sorted({p for v in vozes for p in v["l"].split("-")[1:] if re.fullmatch(r"[A-Z]{2}", p)})
locais = {i: Locale.parse(i) for i in INTERFACE}

idiomas = {}
for c in codigos:
    antigo = atual["idiomas"].get(c, {})
    try:
        nativo = Locale.parse(c).get_display_name(c) or ""
    except Exception:
        nativo = ""
    nomes = {"nativo": antigo.get("nativo") or nativo[:1].upper() + nativo[1:] or c}
    for i, loc in locais.items():
        nome = loc.languages.get(c) or nomes["nativo"]
        nomes[i] = nome[:1].upper() + nome[1:]
    if antigo.get("pt") or antigo.get("nomes", {}).get("pt"):
        nomes["pt"] = antigo.get("pt") or antigo["nomes"]["pt"]
    idiomas[c] = nomes

nomes_regioes = {}
for r in regioes:
    antigo = atual["regioes"].get(r)
    nomes = {i: loc.territories.get(r, r) for i, loc in locais.items()}
    if isinstance(antigo, str):
        nomes["pt"] = antigo
    elif isinstance(antigo, dict) and antigo.get("pt"):
        nomes["pt"] = antigo["pt"]
    nomes_regioes[r] = nomes

with open(caminho, "w", encoding="utf-8") as f:
    json.dump({"idiomas": idiomas, "regioes": nomes_regioes}, f, ensure_ascii=False, indent=1)
print(f"✔ dados/idiomas.json: {len(idiomas)} idiomas e {len(nomes_regioes)} regiões em {len(INTERFACE)} idiomas da interface")
